"""거래 이력, 조회 조건, CSV와 startup Performance 공개 계약을 제공한다."""

# CSV option과 filesystem 경계가 공유하는 성공·실패 타입을 하나의 domain 공개면으로 모은다.
from .csv_export_options import (
    CSVExportError,
    CSVExportIOError,
    CSVExportOptions,
    CSVExportResult,
    CSVExportValidationError,
    CSVPeriod,
    DestinationExistsError,
    NoTradesToExportError,
)

from .performance import InvalidZeroCostBasisError, Performance
from .query import HistoryPeriod, TradeHistoryQuery, TradeSide
from .trade import (
    FeeAssetConversionRequiredError,
    TRADE_RECORD_TYPE,
    TRADE_SCHEMA_VERSION,
    RealizedResult,
    Trade,
    trade_from_json_object,
    trade_to_json_object,
)
from .trade_history import OrderHistoryConflictError, TradeHistory


__all__ = [
    "CSVExportError",
    "CSVExportIOError",
    "CSVExportOptions",
    "CSVExportResult",
    "CSVExportValidationError",
    "CSVPeriod",
    "DestinationExistsError",
    "OrderHistoryConflictError",
    "FeeAssetConversionRequiredError",
    "InvalidZeroCostBasisError",
    "HistoryPeriod",
    "NoTradesToExportError",
    "Performance",
    "RealizedResult",
    "TRADE_RECORD_TYPE",
    "TRADE_SCHEMA_VERSION",
    "Trade",
    "TradeHistory",
    "TradeHistoryQuery",
    "TradeSide",
    "trade_from_json_object",
    "trade_to_json_object",
]  # package 밖에서는 이 목록의 canonical history/CSV 타입만 공개한다.
