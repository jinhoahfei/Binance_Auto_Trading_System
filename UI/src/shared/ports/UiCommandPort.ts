import type {
    BackendTradingStatus,
    CsvExportOptions,
    CsvExportReceipt,
    RegimeType,
    TradeHistoryDetails,
    TradeHistoryQuery,
} from '../contracts';

/**
 * backend가 start/stop 명령을 수락한 직후 반환하는 authoritative lifecycle 결과이다.
 */
export interface TradingCommandReceipt {
    readonly status: BackendTradingStatus;
    readonly session_id: string | null;
    readonly version: number;
}

/**
 * UI 제어 actor가 backend 또는 데스크톱 adapter에 요청할 수 있는 명령 계약이다.
 */
export interface UiCommandPort {
    /**
     * 함수 이름: start_trading()
     * 기능: 선택한 REGIME으로 자동매매 시작을 요청한다.
     * 인자: regime_type -> 적용할 REGIME 유형
     * 반환값: 시작 직후 authoritative lifecycle 결과 Promise
     * 작성 날짜: 2026/08/12
     */
    start_trading(regime_type: RegimeType): Promise<TradingCommandReceipt>;

    /**
     * 함수 이름: stop_trading()
     * 기능: backend가 position/pending 상태를 판정하는 authoritative 자동매매 중지를 요청한다.
     * 인자: 없음
     * 반환값: 중지 분기 직후 authoritative lifecycle 결과 Promise
     * 작성 날짜: 2026/08/12
     */
    stop_trading(): Promise<TradingCommandReceipt>;

    /**
     * 함수 이름: force_sell_and_stop()
     * 기능: position 보유 확인 UI에서도 같은 authoritative 자동매매 중지를 요청한다.
     * 인자: 없음
     * 반환값: backend가 결정한 중지 분기의 lifecycle 결과 Promise
     * 작성 날짜: 2026/08/12
     */
    force_sell_and_stop(): Promise<TradingCommandReceipt>;

    /**
     * 함수 이름: liquidate_recovered_position()
     * 기능: 자동 재개 없이 startup에서 복구한 Position만 전량 청산하도록 요청한다.
     * 인자: 없음
     * 반환값: backend가 확정한 복구 청산 lifecycle 결과 Promise
     * 작성 날짜: 2026/08/24
     */
    liquidate_recovered_position(): Promise<TradingCommandReceipt>;

    /**
     * 함수 이름: apply_regime()
     * 기능: 선택한 REGIME을 향후 거래 판단에 적용하도록 요청한다.
     * 인자: regime_type -> 적용할 REGIME 유형
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    apply_regime(regime_type: RegimeType): Promise<void>;

    /**
     * 함수 이름: update_split_order()
     * 기능: 향후 분할 매수 또는 매도에 사용할 비율 변경을 요청한다.
     * 인자: order_side -> 분할 주문 방향, percentage -> 0부터 100 사이의 비율
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    update_split_order(order_side: 'scale_in' | 'scale_out', percentage: number): Promise<void>;

    /**
     * 함수 이름: load_trade_history()
     * 기능: 기간과 거래 방향 조건에 맞는 거래 내역 조회를 요청한다.
     * 인자: query -> 거래 내역 필터, signal -> 화면 수명주기에 연결된 optional 취소 신호
     * 반환값: 필터된 거래 행과 D-12 고정 범위 요약을 결합한 상세 결과 Promise
     * 작성 날짜: 2026/08/23
     */
    load_trade_history(
        query: TradeHistoryQuery,
        signal?: AbortSignal,
    ): Promise<TradeHistoryDetails>;

    /**
     * 함수 이름: pick_csv_directory()
     * 기능: CSV 저장 폴더를 선택하는 데스크톱 picker를 요청한다.
     * 인자: 없음
     * 반환값: 선택 경로 또는 취소를 나타내는 null Promise
     * 작성 날짜: 2026/08/12
     */
    pick_csv_directory(): Promise<string | null>;

    /**
     * 함수 이름: export_csv()
     * 기능: 검증 완료된 옵션으로 거래 내역 CSV 생성을 요청한다.
     * 인자: options -> CSV 저장 위치·파일명·기간 옵션
     * 반환값: 생성 파일 정보 Promise
     * 작성 날짜: 2026/08/12
     */
    export_csv(options: CsvExportOptions): Promise<CsvExportReceipt>;

    /**
     * 함수 이름: shutdown_application()
     * 기능: 연결 종료와 저장 반영을 포함하는 애플리케이션 종료를 요청한다.
     * 인자: 없음
     * 반환값: 종료 준비 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    shutdown_application(liquidation_confirmed?: boolean): Promise<void>;
}
