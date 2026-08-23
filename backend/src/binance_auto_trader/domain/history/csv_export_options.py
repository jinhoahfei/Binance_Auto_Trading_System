"""CSV 내보내기 option, 성공 결과와 typed 실패 계약을 정의한다."""

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
import unicodedata


# Windows가 확장자와 대소문자를 무시하고 예약하는 device basename을 모두 차단한다.
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "AUX",
        "CON",
        "NUL",
        "PRN",
        *(f"COM{device_number}" for device_number in range(1, 10)),
        *(f"LPT{device_number}" for device_number in range(1, 10)),
    }
)
_WINDOWS_FORBIDDEN_CHARACTERS = frozenset('<>:"|?*')


class CSVPeriod(str, Enum):
    """
    클래스 이름: CSVPeriod
    기능: CSV 조회의 오늘·최근 7일·최근 30일·직접 날짜 범위를 구분한다.
    작성 날짜: 2026/08/23
    """

    TODAY = "TODAY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    CUSTOM = "CUSTOM"


class CSVExportError(Exception):
    """
    클래스 이름: CSVExportError
    기능: CSV option 검증과 파일 생성 실패를 하나의 안정된 예외 계층으로 묶는다.
    작성 날짜: 2026/08/23
    """

    code = "CSV_EXPORT_FAILED"


class CSVExportValidationError(CSVExportError, ValueError):
    """
    클래스 이름: CSVExportValidationError
    기능: backend가 거부한 CSV option 필드와 안전한 사유를 나타낸다.
    작성 날짜: 2026/08/23
    """

    code = "INVALID_CSV_EXPORT_OPTIONS"

    def __init__(self, field: str, reason: str) -> None:
        """
        함수 이름: __init__()
        기능: 잘못된 필드 이름과 사용자 값을 포함하지 않는 사유를 보존한다.
        인자: field -> 검증에 실패한 option 필드 이름
            reason -> 입력 원문과 경로를 포함하지 않는 안정된 실패 사유
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # route가 입력값을 다시 해석하지 않고 control별 오류를 표시할 정보만 보존한다.
        self.field = field
        self.reason = reason
        super().__init__(f"invalid CSV export option: {field}: {reason}")


class NoTradesToExportError(CSVExportError):
    """
    클래스 이름: NoTradesToExportError
    기능: 조회 범위에 내보낼 거래가 없어 파일을 만들지 않았음을 나타낸다.
    작성 날짜: 2026/08/23
    """

    code = "NO_TRADES_TO_EXPORT"

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 경로나 거래 내용을 노출하지 않는 빈 export 오류를 생성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        super().__init__("no trades are available for CSV export")


class DestinationExistsError(CSVExportError):
    """
    클래스 이름: DestinationExistsError
    기능: 기존 destination을 덮어쓰지 않는 CSV 정책 위반을 나타낸다.
    작성 날짜: 2026/08/23
    """

    code = "DESTINATION_EXISTS"

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 실제 destination 경로를 노출하지 않는 충돌 오류를 생성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        super().__init__("CSV export destination already exists")


class CSVExportIOError(CSVExportError):
    """
    클래스 이름: CSVExportIOError
    기능: CSV 생성 중 발생한 filesystem 실패를 경로 비노출 typed 오류로 변환한다.
    작성 날짜: 2026/08/23
    """

    code = "CSV_EXPORT_IO_FAILED"

    def __init__(self, operation: str) -> None:
        """
        함수 이름: __init__()
        기능: 실패한 안전한 filesystem 단계만 보존하고 원래 경로와 예외를 숨긴다.
        인자: operation -> directory, write, fsync 또는 commit 단계 이름
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # UI와 trace에는 OS 오류 원문 대신 경로를 포함하지 않는 단계 이름만 제공한다.
        self.operation = operation
        super().__init__(f"CSV export filesystem operation failed: {operation}")


def _contains_control_character(value: str) -> bool:
    """
    함수 이름: _contains_control_character()
    기능: 문자열에 ASCII 및 Unicode control 문자가 하나라도 있는지 확인한다.
    인자: value -> 검사할 경로 또는 파일명 문자열
    반환값: Unicode category가 Cc인 문자가 있으면 True
    작성 날짜: 2026/08/23
    """
    return any(
        unicodedata.category(character) == "Cc" for character in value
    )  # NUL뿐 아니라 개행과 C1 control도 같은 규칙으로 차단한다.


def _validate_save_location(save_location: object) -> str:
    """
    함수 이름: _validate_save_location()
    기능: filesystem 조회 없이 저장 위치 문자열의 구조만 검증한다.
    인자: save_location -> native picker 또는 transport가 전달한 저장 위치
    반환값: 검증된 원래 저장 위치 문자열
    작성 날짜: 2026/08/23
    """
    # domain은 os/pathlib 없이 타입, 공백과 control 문자만 검증한다.
    if not isinstance(save_location, str):
        raise CSVExportValidationError("save_location", "must_be_a_string")
    if not save_location:
        raise CSVExportValidationError("save_location", "must_not_be_empty")
    if save_location != save_location.strip():
        raise CSVExportValidationError(
            "save_location",
            "must_not_have_outer_whitespace",
        )
    if _contains_control_character(save_location):
        raise CSVExportValidationError(
            "save_location",
            "must_not_contain_control_characters",
        )

    return save_location  # 실제 존재·directory 여부와 권한은 filesystem adapter가 확인한다.


def _validate_and_normalize_file_name(file_name: object) -> str:
    """
    함수 이름: _validate_and_normalize_file_name()
    기능: CSV basename 보안 규칙을 검증하고 누락된 확장자를 정확히 한 번 붙인다.
    인자: file_name -> 사용자가 입력한 CSV 파일명
    반환값: 대소문자를 보존하면서 .csv 확장자를 가진 안전한 basename
    작성 날짜: 2026/08/23
    """
    # 문자열 type과 outer whitespace를 먼저 고정해 암묵적 trim이나 변환을 막는다.
    if not isinstance(file_name, str):
        raise CSVExportValidationError("file_name", "must_be_a_string")
    if not file_name:
        raise CSVExportValidationError("file_name", "must_not_be_empty")
    if file_name != file_name.strip():
        raise CSVExportValidationError(
            "file_name",
            "must_not_have_outer_whitespace",
        )

    # basename을 벗어나는 separator, dot traversal과 모든 control 문자를 거부한다.
    if "/" in file_name or "\\" in file_name:
        raise CSVExportValidationError(
            "file_name",
            "must_not_contain_path_separators",
        )
    if ".." in file_name:
        raise CSVExportValidationError("file_name", "must_not_contain_traversal")
    if _contains_control_character(file_name):
        raise CSVExportValidationError(
            "file_name",
            "must_not_contain_control_characters",
        )
    if any(
        character in _WINDOWS_FORBIDDEN_CHARACTERS
        for character in file_name
    ):
        raise CSVExportValidationError(
            "file_name",
            "must_not_contain_forbidden_characters",
        )
    if file_name.endswith("."):
        raise CSVExportValidationError(
            "file_name",
            "must_not_end_with_a_period",
        )

    # Windows는 첫 확장자 앞의 device 이름을 대소문자와 후속 확장자와 무관하게 예약한다.
    windows_basename = file_name.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_BASENAMES:
        raise CSVExportValidationError(
            "file_name",
            "must_not_use_a_windows_reserved_name",
        )

    # 확장자만 있는 이름에는 실제 basename이 없으므로 빈 이름과 동일하게 거부한다.
    has_csv_extension = file_name.casefold().endswith(".csv")
    if has_csv_extension and len(file_name) == len(".csv"):
        raise CSVExportValidationError(
            "file_name",
            "must_have_a_non_empty_basename",
        )

    return (
        file_name if has_csv_extension else f"{file_name}.csv"
    )  # 이미 있는 대소문자 변형 확장자는 보존하고 중복 추가하지 않는다.


@dataclass(frozen=True, slots=True)
class CSVExportOptions:
    """
    클래스 이름: CSVExportOptions
    기능: backend가 재검증한 저장 위치, 기간, KST 날짜와 CSV basename을 보존한다.
    작성 날짜: 2026/08/23
    """

    save_location: str
    period: CSVPeriod
    start_date: date
    end_date: date
    file_name: str

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: option의 구조와 날짜 순서를 검증하고 파일 확장자를 canonical하게 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # UI 검증과 독립적으로 canonical period와 순수 LocalDate 타입을 다시 확인한다.
        if not isinstance(self.period, CSVPeriod):
            raise CSVExportValidationError(
                "period",
                "must_be_the_canonical_csv_period",
            )
        if type(self.start_date) is not date:
            raise CSVExportValidationError("start_date", "must_be_a_date")
        if type(self.end_date) is not date:
            raise CSVExportValidationError("end_date", "must_be_a_date")
        if self.start_date > self.end_date:
            raise CSVExportValidationError(
                "date_range",
                "start_date_must_not_be_after_end_date",
            )

        # frozen value에는 구조 검증을 통과한 경로와 정확히 한 번 정규화한 파일명만 남긴다.
        normalized_save_location = _validate_save_location(self.save_location)
        normalized_file_name = _validate_and_normalize_file_name(self.file_name)
        object.__setattr__(self, "save_location", normalized_save_location)
        object.__setattr__(self, "file_name", normalized_file_name)

    def resolve_dates(self, current_kst_date: date) -> tuple[date, date]:
        """
        함수 이름: resolve_dates()
        기능: CSV preset을 현재 KST LocalDate 기준 양끝 포함 시작일과 종료일로 변환한다.
        인자: current_kst_date -> 호출 시점의 Asia/Seoul 현재 LocalDate
        반환값: 양끝 날짜를 포함하는 start_date와 end_date tuple
        작성 날짜: 2026/08/23
        """
        # datetime이나 문자열을 LocalDate로 암묵 변환하지 않고 순수 date만 허용한다.
        if type(current_kst_date) is not date:
            raise CSVExportValidationError(
                "current_kst_date",
                "must_be_a_date",
            )
        if self.period is CSVPeriod.CUSTOM:
            return (
                self.start_date,
                self.end_date,
            )  # CUSTOM은 사용자가 지정한 inclusive 범위를 정확히 보존한다.

        # TODAY/7일/30일은 현재 날짜를 끝으로 고정하고 각각 0/6/29일을 되돌아간다.
        lookback_days = {
            CSVPeriod.TODAY: 0,
            CSVPeriod.WEEKLY: 6,
            CSVPeriod.MONTHLY: 29,
        }[self.period]
        return (
            current_kst_date - timedelta(days=lookback_days),
            current_kst_date,
        )


@dataclass(frozen=True, slots=True)
class CSVExportResult:
    """
    클래스 이름: CSVExportResult
    기능: 성공한 CSV의 absolute 경로 문자열과 양수 data row 수를 불변으로 보존한다.
    작성 날짜: 2026/08/23
    """

    file_path: str
    exported_row_count: int

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 성공 결과가 비어 있지 않은 경로와 양수 정수 row count인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # Domain은 filesystem을 import하지 않고 문자열 구조를, adapter/transport는 absolute를 보장한다.
        if not isinstance(self.file_path, str):
            raise TypeError("file_path must be a string")
        if not self.file_path or self.file_path != self.file_path.strip():
            raise ValueError("file_path must be non-empty without outer whitespace")
        if _contains_control_character(self.file_path):
            raise ValueError("file_path must not contain control characters")
        if type(self.exported_row_count) is not int:
            raise TypeError("exported_row_count must be an integer")
        if self.exported_row_count <= 0:
            raise ValueError("exported_row_count must be greater than zero")
