import type {
    CsvExportOptions,
    CsvExportReceipt,
    RegimeType,
    TradeHistoryQuery,
    TradeRecord,
} from '../contracts';

/**
 * UI 제어 actor가 backend 또는 데스크톱 adapter에 요청할 수 있는 명령 계약이다.
 */
export interface UiCommandPort {
    /**
     * 함수 이름: start_trading()
     * 기능: 선택한 REGIME으로 자동매매 시작을 요청한다.
     * 인자: regime_type -> 적용할 REGIME 유형
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    start_trading(regime_type: RegimeType): Promise<void>;

    /**
     * 함수 이름: stop_trading()
     * 기능: 포지션 매도 없이 자동매매 중지를 요청한다.
     * 인자: 없음
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    stop_trading(): Promise<void>;

    /**
     * 함수 이름: force_sell_and_stop()
     * 기능: 포지션 강제 매도 후 자동매매 중지를 요청한다.
     * 인자: 없음
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    force_sell_and_stop(): Promise<void>;

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
     * 인자: query -> 거래 내역 필터
     * 반환값: 정규화된 거래 내역 Promise
     * 작성 날짜: 2026/08/12
     */
    load_trade_history(query: TradeHistoryQuery): Promise<ReadonlyArray<TradeRecord>>;

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
    shutdown_application(): Promise<void>;
}
