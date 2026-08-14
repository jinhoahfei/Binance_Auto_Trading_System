import { createActor } from 'xstate';
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
    it('TD4-05/VR-07: 저장 위치가 없으면 export 명령을 차단하고 필드를 표시한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_csv_export_machine(command_adapter, {
            today: '2026-08-12',
        }));

        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'EXPORT_CSV' });

        expect(actor.getSnapshot().matches('editing')).toBe(true);
        expect(actor.getSnapshot().context.validation_errors.directory).not.toBeNull();
        expect(command_adapter.command_records).toHaveLength(0);
        actor.stop();
    });

    it('빈 문자열 경로는 폴더 선택 결과로 수락하지 않는다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.selected_directory = '   ';
        const actor = createActor(create_csv_export_machine(command_adapter, {
            today: '2026-08-12',
        }));

        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });
        await wait_for_actor_settlement();
        actor.send({ type: 'EXPORT_CSV' });

        expect(actor.getSnapshot().context.directory).toBeNull();
        expect(actor.getSnapshot().context.validation_errors.directory).not.toBeNull();
        expect(command_adapter.command_records.filter((record) => record.name === 'export_csv')).toHaveLength(0);
        actor.stop();
    });

    it('ER-16: 달력 외부 클릭은 CSV dialog를 유지하고 calendar만 닫는다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_csv_export_machine(command_adapter, {
            today: '2026-08-12',
        }));

        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_DATE' });
        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });
        actor.send({ type: 'START_DATE_CALENDAR_OUTSIDE_CLICKED' });

        expect(actor.getSnapshot().matches('editing')).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBeNull();
        actor.stop();
    });

    it('CR2-02/CR2-04/CR2-14: 기간 preset과 사용자 지정 달력 상태를 명시적으로 전이한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_csv_export_machine(command_adapter, {
            today: '2026-08-12',
        }));

        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_WEEKLY_HISTORY' });
        expect(actor.getSnapshot().matches({ editing: { period: 'weekly' } })).toBe(true);

        actor.send({ type: 'SELECT_CSV_DATE' });
        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });
        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'start_calendar' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBe('start_date');
        actor.stop();
    });

    it('CR2-16/CR2-18: 날짜 선택은 달력을 유지하고 외부 클릭에서만 닫는다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_csv_export_machine(command_adapter, {
            today: '2026-08-12',
        }));

        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_DATE' });
        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });
        actor.send({ type: 'START_DATE_SELECTED', date: '2026-08-01' });

        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'start_calendar' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.start_date).toBe('2026-08-01');

        actor.send({ type: 'START_DATE_CALENDAR_OUTSIDE_CLICKED' });
        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'idle' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBeNull();
        actor.stop();
    });

    it('CR2-14/CR2-15: 열린 달력에서 반대 날짜 필드를 누르면 대상 달력으로 교체한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_csv_export_machine(command_adapter, {
            today: '2026-08-12',
        }));

        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_DATE' });
        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });
        actor.send({ type: 'START_CSV_FINISH_DATE_SELECTION' });

        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'end_calendar' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBe('end_date');

        actor.send({ type: 'START_CSV_START_DATE_SELECTION' });
        expect(actor.getSnapshot().matches({
            editing: { period: { custom: 'start_calendar' } },
        })).toBe(true);
        expect(actor.getSnapshot().context.calendar_target).toBe('start_date');
        actor.stop();
    });

    it('CR1-02~04: 폴더 picker가 열려도 기간과 파일명 region을 보존한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_csv_export_machine(command_adapter, {
            today: '2026-08-12',
        }));

        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SELECT_CSV_MONTHLY_HISTORY' });
        actor.send({ type: 'FILE_NAME_CLICKED' });
        actor.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });

        expect(actor.getSnapshot().matches({
            editing: {
                file_browser: 'opened',
                period: 'monthly',
                file_name: 'editing',
            },
        })).toBe(true);
        await wait_for_actor_settlement();

        expect(actor.getSnapshot().matches({
            editing: {
                file_browser: 'closed',
                period: 'monthly',
                file_name: 'editing',
            },
        })).toBe(true);
        actor.stop();
    });

    it('TD4-04/TD4-06: 유효한 draft는 한 번 내보내고 완료 receipt를 저장한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_csv_export_machine(command_adapter, {
            today: '2026-08-12',
        }));

        actor.start();
        actor.send({ type: 'CSV_EXPORT_CLICKED' });
        actor.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });
        await wait_for_actor_settlement();
        actor.send({ type: 'EXPORT_CSV' });
        actor.send({ type: 'EXPORT_CSV' });
        await wait_for_actor_settlement();

        expect(actor.getSnapshot().matches('complete')).toBe(true);
        expect(actor.getSnapshot().context.receipt?.exported_row_count).toBe(3);
        expect(command_adapter.command_records.filter((record) => record.name === 'export_csv')).toHaveLength(1);
        actor.stop();
    });
});
