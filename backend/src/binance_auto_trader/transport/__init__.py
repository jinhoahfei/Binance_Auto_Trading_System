"""Backend loopback transport의 공개 server, event와 contract API를 제공한다."""

# 외부 composition과 UI contract tooling에 필요한 안정된 transport 경계만 다시 공개한다.
from .app import (
    LoopbackTransportApplication,
    LoopbackTransportServer,
    ServerDescriptor,
    create_account_update_observer,
    create_loopback_transport_application,
    create_trade_history_update_observer,
    read_session_token_from_fd,
    run_transport_process,
)
from .contracts import (
    MAX_HTTP_BODY_BYTES,
    MAX_WEBSOCKET_FRAME_BYTES,
    SCHEMA_VERSION,
    TransportContractError,
    TransportResponse,
    build_snapshot_dto,
    decimal_from_wire,
    datetime_to_wire,
    decimal_to_wire,
    map_account_snapshot,
    map_trading_snapshot,
    ratio_from_wire,
    regime_from_wire,
    regime_to_wire,
    require_command_fields,
    require_expected_version,
    render_typescript_contracts,
)
from .event_stream import (
    BackendEventEnvelope,
    BackendEventStream,
    ReplayBatch,
)


__all__ = [  # wildcard import도 명시된 Phase 5 공개 API 밖으로 확장되지 않는다.
    "BackendEventEnvelope",
    "BackendEventStream",
    "LoopbackTransportApplication",
    "LoopbackTransportServer",
    "MAX_HTTP_BODY_BYTES",
    "MAX_WEBSOCKET_FRAME_BYTES",
    "ReplayBatch",
    "SCHEMA_VERSION",
    "ServerDescriptor",
    "TransportContractError",
    "TransportResponse",
    "build_snapshot_dto",
    "create_account_update_observer",
    "create_loopback_transport_application",
    "create_trade_history_update_observer",
    "decimal_from_wire",
    "datetime_to_wire",
    "decimal_to_wire",
    "map_account_snapshot",
    "map_trading_snapshot",
    "read_session_token_from_fd",
    "ratio_from_wire",
    "regime_from_wire",
    "regime_to_wire",
    "require_command_fields",
    "require_expected_version",
    "render_typescript_contracts",
    "run_transport_process",
]
