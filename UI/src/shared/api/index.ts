// backend 통신 모듈의 공개 타입과 진입점을 한 경로로 재수출한다.

export {
    BackendAdapterError,
    BackendUiAdapter,
    validate_connection_descriptor,
    type BackendConnectionDescriptor,
    type BackendUiAdapterCallbacks,
    type BackendUiAdapterDependencies,
    type BackendWebSocket,
} from './BackendUiAdapter';
export {
    BackendCommandError,
    BackendContractError,
    decode_backend_http_envelope,
    is_backend_regime_type,
    map_backend_event_to_intents,
    map_backend_snapshot,
    map_trade_history_summary,
    map_trade_record,
    parse_backend_web_socket_message,
    validate_account_snapshot,
    validate_backend_snapshot,
    validate_performance_snapshot,
    validate_trade_snapshot,
    type MappedBackendSnapshot,
    type ParsedBackendWebSocketMessage,
} from './backendEventMapper';
