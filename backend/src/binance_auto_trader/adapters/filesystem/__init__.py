"""CSV filesystem adapter의 공개 계약을 제공한다."""

# Controller와 bootstrap은 구체 module 경로 대신 adapter package의 단일 공개 class를 사용한다.
from .csv_file_gateway import CSVFileGateway


__all__ = ["CSVFileGateway"]  # wildcard import에도 검증된 concrete writer만 노출한다.
