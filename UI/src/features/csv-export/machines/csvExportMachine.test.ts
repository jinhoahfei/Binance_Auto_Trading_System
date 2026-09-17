import { create_feature_test_actor } from '../../../shared/testing/createFeatureTestController';
import { describe, expect, it } from 'vitest';
import { FakeUiCommandAdapter } from '../../../shared/testing';
import { create_csv_export_machine } from './csvExportMachine';


/**
 * 함수 이름: wait_for_actor_settlement()
 * 기능: CSV picker 또는 export Promise 결과가 snapshot에 반영될 때까지 기다린다.
 * 인자: 없음
 * 반환값: 다음 event loop turn에서 완료되는 Promise
 * 작성 날짜: 2026/08/12
 */
async function wait_for_actor_settlement(): Promise<void> {
    await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
    });
}

describe('csvExportMachine', () => {
    it('dialog를 다시 열면 KST 날짜 source로 today·preset·기본 파일명을 갱신한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        let current_kst_date = '2026-08-12';
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: current_kst_date,
            get_current_kst_date: () => current_kst_date,
        });

        // CSV_EXPORT_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context).toMatchObject({
            today: '2026-08-12',
            start_date: '2026-08-12',
            end_date: '2026-08-12',
            file_name: 'binance_trades_2026-08-12.csv',
        });

        // CLOSE_CSV_EXPORT_POPUP → CSV_EXPORT_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'CLOSE_CSV_EXPORT_POPUP' });

        // KST 자정이 지난 뒤 같은 actor를 다시 열면 모든 날짜 기반 기본값이 새 날짜를 공유한다.
        current_kst_date = '2026-08-13';
        actor.send({ type: 'CSV_EXPORT_CLICKED' });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context).toMatchObject({
            today: '2026-08-13',
            start_date: '2026-08-13',
            end_date: '2026-08-13',
            file_name: 'binance_trades_2026-08-13.csv',
            file_name_draft: 'binance_trades_2026-08-13.csv',
        });

        // SELECT_CSV_WEEKLY_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'SELECT_CSV_WEEKLY_HISTORY' });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context).toMatchObject({
            start_date: '2026-08-07',
            end_date: '2026-08-13',
        });

        // SELECT_CSV_MONTHLY_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'SELECT_CSV_MONTHLY_HISTORY' });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context).toMatchObject({
            start_date: '2026-07-15',
            end_date: '2026-08-13',
        });

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('test_csv_export_clicked_while_editing_is_ignored: 열린 dialog를 중복 초기화하지 않는다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // CSV_EXPORT_CLICKED → SELECT_CSV_WEEKLY_HISTORY → CSV_EXPORT_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_WEEKLY_HISTORY' });
        actor.send({ type: 'CSV_EXPORT_CLICKED' });

        // Editing 상태의 중복 click은 선택 기간과 command 기록을 바꾸지 않아야 한다.
        expect(actor.getSnapshot().matches({ editing: { period: 'weekly' } })).toBe(true);
        expect(command_adapter.command_records).toHaveLength(0);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('TD4-05/VR-07: 저장 위치가 없으면 export 명령을 차단하고 필드를 표시한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // CSV_EXPORT_CLICKED → EXPORT_CSV 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'EXPORT_CSV' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('editing')).toBe(true);
        expect(actor.getSnapshot().context.validation_errors.directory).not.toBeNull();
        expect(command_adapter.command_records).toHaveLength(0);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('빈 문자열 경로는 폴더 선택 결과로 수락하지 않는다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.selected_directory = '   ';
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // CSV_EXPORT_CLICKED → SAVE_LOCATION_SELECT_CLICKED → EXPORT_CSV 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });
        await wait_for_actor_settlement();
        actor.send({ type: 'EXPORT_CSV' });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context.directory).toBeNull();
        expect(actor.getSnapshot().context.validation_errors.directory).not.toBeNull();
        expect(command_adapter.command_records.filter((record) => record.name === 'export_csv')).toHaveLength(0);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('CR1-03/CR-12: picker 취소는 기존 선택 경로를 유지하고 export를 시작하지 않는다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.selected_directory = '/Users/demo/FirstExport';
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // CSV_EXPORT_CLICKED → SAVE_LOCATION_SELECT_CLICKED → SAVE_LOCATION_SELECT_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });
        await wait_for_actor_settlement();

        // 두 번째 native picker의 null은 사용자 취소이므로 기존 directory를 교체하지 않는다.
        command_adapter.selected_directory = null;
        actor.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });
        await wait_for_actor_settlement();

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context.directory).toBe('/Users/demo/FirstExport');
        expect(command_adapter.command_records.filter((record) => record.name === 'export_csv')).toHaveLength(0);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('ER-16: 달력 외부 클릭은 CSV dialog를 유지하고 calendar만 닫는다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_DATE' });
        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });
        actor.send({ type: 'START_DATE_CALENDAR_OUTSIDE_CLICKED' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('editing')).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBeNull();

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('CR2-02/CR2-04/CR2-14: 기간 preset과 사용자 지정 달력 상태를 명시적으로 전이한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // CSV_EXPORT_CLICKED → SELECT_CSV_WEEKLY_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_WEEKLY_HISTORY' });
        expect(actor.getSnapshot().matches({ editing: { period: 'weekly' } })).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // SELECT_CSV_DATE → START_CSV_START_DATE_SELECTION 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'SELECT_CSV_DATE' });
        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'start_calendar' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBe('start_date');

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('CR2-16/CR2-18: 날짜 선택은 달력을 유지하고 외부 클릭에서만 닫는다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_DATE' });
        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });
        actor.send({ type: 'START_DATE_SELECTED', date: '2026-08-01' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'start_calendar' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.start_date).toBe('2026-08-01');

        // START_DATE_CALENDAR_OUTSIDE_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'START_DATE_CALENDAR_OUTSIDE_CLICKED' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'idle' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBeNull();

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('CR2-14/CR2-15: 열린 달력에서 반대 날짜 필드를 누르면 대상 달력으로 교체한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_DATE' });
        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });
        actor.send({ type: 'START_CSV_FINISH_DATE_SELECTION' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'end_calendar' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBe('end_date');

        // START_CSV_START_DATE_SELECTION 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'start_calendar' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBe('start_date');

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('CR1-02~04: 폴더 picker가 열려도 기간과 파일명 region을 보존한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_MONTHLY_HISTORY' });
        actor.send({ type: 'FILE_NAME_CLICKED' });
        actor.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches({
            editing: {
                file_browser: 'opened',
                period: 'monthly',
                file_name: 'editing',
            },
        })).toBe(true);

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        await wait_for_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches({
            editing: {
                file_browser: 'closed',
                period: 'monthly',
                file_name: 'editing',
            },
        })).toBe(true);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('TD4-04/TD4-06: 유효한 draft는 한 번 내보내고 완료 receipt를 저장한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });
        await wait_for_actor_settlement();
        actor.send({ type: 'EXPORT_CSV' });
        actor.send({ type: 'EXPORT_CSV' });
        await wait_for_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('complete')).toBe(true);
        expect(actor.getSnapshot().context.receipt?.exported_row_count).toBe(3);
        expect(actor.getSnapshot().context.receipt?.file_path).toBe(
            '/Users/demo/Exports/binance_trades_2026-08-12.csv',
        );
        expect(command_adapter.command_records.filter((record) => record.name === 'export_csv')).toHaveLength(1);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('TD4-07/TD4-08: export 실패는 실제 오류와 option을 유지해 수정 후 재시도하게 한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure('export_csv', new Error('Disk write failed safely.'));
        const actor = create_feature_test_actor(create_csv_export_machine, command_adapter, {
            today: '2026-08-12',
        });

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });
        await wait_for_actor_settlement();
        actor.send({ type: 'SELECT_CSV_MONTHLY_HISTORY' });
        actor.send({ type: 'FILE_NAME_CLICKED' });
        actor.send({ type: 'FILE_NAME_CHANGED', file_name: 'monthly-history' });
        actor.send({ type: 'ENTER_KEY_TYPED' });
        actor.send({ type: 'EXPORT_CSV' });
        await wait_for_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('error')).toBe(true);
        expect(actor.getSnapshot().context).toMatchObject({
            directory: '/Users/demo/Exports',
            period: 'last30days',
            start_date: '2026-07-14',
            end_date: '2026-08-12',
            file_name: 'monthly-history.csv',
            file_name_draft: 'monthly-history.csv',
            command_error: {
                code: 'CSV_EXPORT_FAILED',
                message: 'Disk write failed safely.',
            },
        });

        // CSV_EXPORT_ERROR_CONFIRMED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'CSV_EXPORT_ERROR_CONFIRMED' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('editing')).toBe(true);
        expect(actor.getSnapshot().context.directory).toBe('/Users/demo/Exports');
        expect(actor.getSnapshot().context.file_name).toBe('monthly-history.csv');

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });
});
