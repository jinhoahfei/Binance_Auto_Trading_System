import type {
    CsvExportOptions,
    CsvExportReceipt,
    RegimeType,
    TradeHistoryQuery,
    TradeRecord,
} from '../contracts';
import type { UiCommandPort } from '../ports';
import { TRADE_RECORD_FIXTURES } from './fixtures';

type FakeCommandName =
    | 'start_trading'
    | 'stop_trading'
    | 'force_sell_and_stop'
    | 'apply_regime'
    | 'update_split_order'
    | 'load_trade_history'
    | 'pick_csv_directory'
    | 'export_csv'
    | 'shutdown_application';

/**
 * 테스트가 adapter 호출 순서와 인자를 검증할 때 사용하는 기록이다.
 */
export interface FakeCommandRecord {
    readonly name: FakeCommandName;
    readonly payload: unknown;
}

/**
 * 클래스 이름: FakeUiCommandAdapter
 * 기능: backend가 없는 UI 개발과 상태 머신 테스트를 위해 결정적 명령 결과를 제공한다.
 * 작성 날짜: 2026/08/12
 */
export class FakeUiCommandAdapter implements UiCommandPort {
    readonly command_records: Array<FakeCommandRecord> = [];
    trade_history: ReadonlyArray<TradeRecord> = TRADE_RECORD_FIXTURES;
    selected_directory: string | null = '/Users/demo/Exports';
    exported_receipt: CsvExportReceipt = {
        file_path: '/Users/demo/Exports/binance_trades_2026-08-12.csv',
        exported_row_count: TRADE_RECORD_FIXTURES.length,
    };

    private readonly failure_queues = new Map<FakeCommandName, Array<Error>>();

    /**
     * 함수 이름: queue_failure()
     * 기능: 지정한 다음 명령 호출이 실패하도록 오류를 대기열에 추가한다.
     * 인자: command_name -> 실패시킬 명령 이름, error -> 반환할 오류
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    queue_failure(command_name: FakeCommandName, error: Error): void {
        const queued_failures = this.failure_queues.get(command_name) ?? [];

        queued_failures.push(error);
        this.failure_queues.set(command_name, queued_failures);
    }

    /**
     * 함수 이름: start_trading()
     * 기능: 자동매매 시작 명령을 기록하고 예약된 실패가 있으면 반환한다.
     * 인자: regime_type -> 적용할 REGIME 유형
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    async start_trading(regime_type: RegimeType): Promise<void> {
        this.record_command('start_trading', { regime_type });
        this.throw_queued_failure('start_trading');
    }

    /**
     * 함수 이름: stop_trading()
     * 기능: 포지션 매도 없이 자동매매를 중단하는 명령을 기록한다.
     * 인자: 없음
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    async stop_trading(): Promise<void> {
        this.record_command('stop_trading', null);
        this.throw_queued_failure('stop_trading');
    }

    /**
     * 함수 이름: force_sell_and_stop()
     * 기능: 보유 포지션 강제 매도 후 중지 명령을 기록한다.
     * 인자: 없음
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    async force_sell_and_stop(): Promise<void> {
        this.record_command('force_sell_and_stop', null);
        this.throw_queued_failure('force_sell_and_stop');
    }

    /**
     * 함수 이름: apply_regime()
     * 기능: REGIME 적용 명령을 기록한다.
     * 인자: regime_type -> 적용할 REGIME 유형
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    async apply_regime(regime_type: RegimeType): Promise<void> {
        this.record_command('apply_regime', { regime_type });
        this.throw_queued_failure('apply_regime');
    }

    /**
     * 함수 이름: update_split_order()
     * 기능: 다음 분할 주문 비율 변경 명령을 기록한다.
     * 인자: order_side -> 분할 매수 또는 분할 매도, percentage -> 적용할 비율
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    async update_split_order(
        order_side: 'scale_in' | 'scale_out',
        percentage: number,
    ): Promise<void> {
        this.record_command('update_split_order', { order_side, percentage });
        this.throw_queued_failure('update_split_order');
    }

    /**
     * 함수 이름: load_trade_history()
     * 기능: 결정적 거래 내역을 조회하고 전달받은 필터에 맞게 반환한다.
     * 인자: query -> 기간 및 거래 방향 필터
     * 반환값: 필터가 적용된 거래 내역 Promise
     * 작성 날짜: 2026/08/12
     */
    async load_trade_history(query: TradeHistoryQuery): Promise<ReadonlyArray<TradeRecord>> {
        this.record_command('load_trade_history', query);
        this.throw_queued_failure('load_trade_history');

        const latest_record_time = this.trade_history.reduce((latest_time, trade_record) => {
            return Math.max(latest_time, new Date(trade_record.occurred_at).getTime());
        }, 0);
        const period_in_days = query.period === 'today'
            ? 1
            : query.period === 'last7days'
                ? 7
                : query.period === 'last30days'
                    ? 30
                    : Number.POSITIVE_INFINITY;
        const latest_record_date = new Date(latest_record_time);
        const latest_day_start = Date.UTC(
            latest_record_date.getUTCFullYear(),
            latest_record_date.getUTCMonth(),
            latest_record_date.getUTCDate(),
        );
        const period_start_time = latest_day_start - ((period_in_days - 1) * 86_400_000);

        return this.trade_history.filter((trade_record) => {
            const is_matching_side = query.side === 'all' || trade_record.side === query.side;
            const is_in_period = new Date(trade_record.occurred_at).getTime() >= period_start_time;

            return is_matching_side && is_in_period;
        });
    }

    /**
     * 함수 이름: pick_csv_directory()
     * 기능: 결정적으로 설정된 폴더 선택 결과를 반환한다.
     * 인자: 없음
     * 반환값: 선택 경로 또는 취소를 나타내는 null Promise
     * 작성 날짜: 2026/08/12
     */
    async pick_csv_directory(): Promise<string | null> {
        this.record_command('pick_csv_directory', null);
        this.throw_queued_failure('pick_csv_directory');

        return this.selected_directory;
    }

    /**
     * 함수 이름: export_csv()
     * 기능: CSV 내보내기 옵션을 기록하고 결정적 완료 결과를 반환한다.
     * 인자: options -> 검증 완료된 CSV 내보내기 옵션
     * 반환값: 생성 파일 정보 Promise
     * 작성 날짜: 2026/08/12
     */
    async export_csv(options: CsvExportOptions): Promise<CsvExportReceipt> {
        this.record_command('export_csv', options);
        this.throw_queued_failure('export_csv');

        return this.exported_receipt;
    }

    /**
     * 함수 이름: shutdown_application()
     * 기능: 저장 반영과 연결 종료를 포함하는 애플리케이션 종료 명령을 기록한다.
     * 인자: 없음
     * 반환값: 명령 완료 Promise
     * 작성 날짜: 2026/08/12
     */
    async shutdown_application(): Promise<void> {
        this.record_command('shutdown_application', null);
        this.throw_queued_failure('shutdown_application');
    }

    /**
     * 함수 이름: record_command()
     * 기능: 실행된 fake 명령과 payload를 순서대로 저장한다.
     * 인자: command_name -> 명령 이름, payload -> 명령 인자
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private record_command(command_name: FakeCommandName, payload: unknown): void {
        this.command_records.push({ name: command_name, payload });
    }

    /**
     * 함수 이름: throw_queued_failure()
     * 기능: 지정한 명령에 예약된 첫 오류가 있으면 제거한 뒤 발생시킨다.
     * 인자: command_name -> 확인할 명령 이름
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private throw_queued_failure(command_name: FakeCommandName): void {
        const queued_failures = this.failure_queues.get(command_name);
        const queued_failure = queued_failures?.shift();

        if (queued_failure !== undefined) {
            throw queued_failure;
        }
    }
}
