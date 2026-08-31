import { describe, expect, it } from 'vitest';

import { FakeUiCommandAdapter } from '../../shared/testing';
import { UiApplicationFacade } from './UiApplicationFacade';

/**
 * 함수 이름: wait_for_facade_settlement()
 * 기능: facade가 시작한 actor Promise와 후속 상태 전이를 다음 event loop까지 기다린다.
 * 인자: 없음
 * 반환값: 비동기 명령 정착 후 완료되는 Promise
 * 작성 날짜: 2026/08/25
 */
async function wait_for_facade_settlement(): Promise<void> {
    await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
    });
}

describe('UiApplicationFacade Phase 13 negative Communication trace', () => {
    /** Communication Case 1 메시지 6.1의 command 실패가 적용 성공으로 보이지 않는지 검증한다. */
    it('test_regime_selection_dispatch_failure_stays_unapplied: REGIME 실패를 보존한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure('apply_regime', new Error('regime unavailable'));
        const facade = new UiApplicationFacade(command_adapter, { today: '2026-08-25' });

        facade.start();
        expect(facade.dispatch({ type: 'REGIME_TYPE_CLICKED', regime: 'type0' })).toBe(true);
        expect(facade.dispatch({ type: 'REGIME_CHANGE_CONFIRMED' })).toBe(true);
        await wait_for_facade_settlement();

        // Adapter failure 뒤에도 후보를 적용값으로 승격하거나 modal을 성공 종료하지 않는다.
        expect(facade.get_view_model().regime.applied).toBeNull();
        expect(facade.get_view_model().regime.error?.message).toBe('regime unavailable');
        expect(command_adapter.command_records).toEqual([{
            name: 'apply_regime',
            payload: { regime_type: 'type0' },
        }]);
        facade.stop();
    });

    /** Communication Case 3 메시지 1.1·2.1의 initial/filter 조회 실패를 직접 검증한다. */
    it('test_history_dispatch_initial_and_filter_failures_are_fail_closed: 조회 실패를 표시한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure('load_trade_history', new Error('initial history unavailable'));
        const facade = new UiApplicationFacade(command_adapter, { today: '2026-08-25' });

        facade.start();
        expect(facade.dispatch({ type: 'SHOW_TRADE_HISTORY' })).toBe(true);
        await wait_for_facade_settlement();
        expect(facade.get_view_model().trade_history.status).toBe('failed');
        expect(facade.get_view_model().trade_history.error?.message)
            .toBe('initial history unavailable');

        // 실패 화면의 filter 변경은 새 결합 query를 사용하되 두 번째 실패도 ready로 축소하지 않는다.
        command_adapter.queue_failure('load_trade_history', new Error('filtered history unavailable'));
        expect(facade.dispatch({
            type: 'HISTORY_PERIOD_SELECTED',
            period: 'last30days',
        })).toBe(true);
        await wait_for_facade_settlement();
        expect(facade.get_view_model().trade_history.status).toBe('failed');
        expect(facade.get_view_model().trade_history.error?.message)
            .toBe('filtered history unavailable');
        expect(command_adapter.command_records.at(-1)).toEqual({
            name: 'load_trade_history',
            payload: { period: 'last30days', side: 'all' },
        });
        facade.stop();
    });

    /** Communication Case 4 메시지 1.1·3.1·4.1의 modal/option/validation 음성 경계를 검증한다. */
    it('test_csv_facade_rejects_conflict_closed_edits_and_invalid_export: 잘못된 dispatch를 차단한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, { today: '2026-08-25' });

        facade.start();
        facade.dispatch({ type: 'REGIME_TYPE_CLICKED', regime: 'type0' });
        expect(facade.dispatch({ type: 'OPEN_CSV_EXPORT' })).toBe(false);
        expect(facade.get_view_model().active_modal).toBe('regime_change_confirmation');
        facade.dispatch({ type: 'REGIME_CHANGE_CANCELED' });

        // 닫힌 CSV actor는 option patch를 무시하고, 열린 invalid draft는 export port를 호출하지 않는다.
        facade.dispatch({ type: 'CSV_PERIOD_SELECTED', period: 'last30days' });
        expect(facade.get_view_model().csv_export.period).toBe('today');
        expect(facade.dispatch({ type: 'OPEN_CSV_EXPORT' })).toBe(true);
        facade.dispatch({ type: 'CSV_EXPORT_SUBMITTED' });
        expect(facade.get_view_model().csv_export.validation_errors.directory).not.toBeNull();
        expect(command_adapter.command_records.filter((record) => {
            return record.name === 'export_csv';
        })).toHaveLength(0);
        facade.stop();
    });

    /** Communication Case 4 메시지 2.1의 picker failure가 기존 option을 성공 처리하지 않는지 검증한다. */
    it('test_csv_picker_dispatch_failure_keeps_directory_unselected: picker 실패를 보존한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure('pick_csv_directory', new Error('picker unavailable'));
        const facade = new UiApplicationFacade(command_adapter, { today: '2026-08-25' });

        facade.start();
        facade.dispatch({ type: 'OPEN_CSV_EXPORT' });
        expect(facade.dispatch({ type: 'CSV_DIRECTORY_SELECT_CLICKED' })).toBe(true);
        await wait_for_facade_settlement();

        // Native picker 실패는 null 선택 성공과 구분해 command error를 남기고 경로를 갱신하지 않는다.
        expect(facade.get_view_model().csv_export.directory).toBeNull();
        expect(facade.get_view_model().csv_export.command_error?.message).toBe('picker unavailable');
        expect(command_adapter.command_records.at(-1)).toEqual({
            name: 'pick_csv_directory',
            payload: null,
        });
        facade.stop();
    });
});
