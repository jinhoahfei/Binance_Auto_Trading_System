import { assign, fromPromise, setup } from 'xstate';
import type {
    CsvExportOptions,
    CsvExportReceipt,
    CsvPeriod,
    LocalDateString,
    UiCommandFailure,
} from '../../../shared/contracts';
import type { UiCommandPort } from '../../../shared/ports';
import { to_ui_command_failure } from '../../../shared/errors';

export interface CsvValidationErrors {
    readonly directory: string | null;
    readonly file_name: string | null;
    readonly date_range: string | null;
}

export interface CsvExportMachineContext {
    readonly today: LocalDateString;
    readonly directory: string | null;
    readonly period: CsvPeriod;
    readonly start_date: LocalDateString | null;
    readonly end_date: LocalDateString | null;
    readonly file_name: string;
    readonly file_name_draft: string;
    readonly file_name_committed: boolean;
    readonly calendar_target: 'start_date' | 'end_date' | null;
    readonly validation_errors: CsvValidationErrors;
    readonly command_error: UiCommandFailure | null;
    readonly receipt: CsvExportReceipt | null;
}

export interface CsvExportMachineOptions {
    readonly today: LocalDateString;
    readonly default_file_name?: string;
    // 생략하면 초기 today를 고정 source로 사용해 demo와 test의 결정성을 보존한다.
    readonly get_current_kst_date?: () => LocalDateString;
}

export type CsvExportMachineEvent =
    | { readonly type: 'CSV_EXPORT_CLICKED' }
    | { readonly type: 'CLOSE_CSV_EXPORT_POPUP' }
    | { readonly type: 'CSV_DIALOG_OUTSIDE_CLICKED' }
    | { readonly type: 'SAVE_LOCATION_SELECT_CLICKED' }
    | { readonly type: 'SELECT_CSV_TODAY_HISTORY' }
    | { readonly type: 'SELECT_CSV_WEEKLY_HISTORY' }
    | { readonly type: 'SELECT_CSV_MONTHLY_HISTORY' }
    | { readonly type: 'SELECT_CSV_DATE' }
    | { readonly type: 'START_CSV_START_DATE_SELECTION' }
    | { readonly type: 'START_CSV_FINISH_DATE_SELECTION' }
    | { readonly type: 'START_DATE_SELECTED'; readonly date: LocalDateString }
    | { readonly type: 'FINISH_DATE_SELECTED'; readonly date: LocalDateString }
    | { readonly type: 'START_DATE_CALENDAR_OUTSIDE_CLICKED' }
    | { readonly type: 'FINISH_DATE_CALENDAR_OUTSIDE_CLICKED' }
    | { readonly type: 'FILE_NAME_CLICKED' }
    | { readonly type: 'FILE_NAME_CHANGED'; readonly file_name: string }
    | { readonly type: 'ENTER_KEY_TYPED' }
    | { readonly type: 'FILE_NAME_INPUT_FOCUS_LOST' }
    | { readonly type: 'EXPORT_CSV' }
    | { readonly type: 'CSV_EXPORT_ERROR_CONFIRMED' }
    | { readonly type: 'ACCEPT_CLOSE_ALL_POPUP' };

/**
 * 함수 이름: shift_date()
 * 기능: ISO 로컬 날짜를 지정한 일수만큼 이동해 동일 형식으로 반환한다.
 * 인자: date -> 기준 YYYY-MM-DD 날짜, day_offset -> 이동할 일수
 * 반환값: 이동된 YYYY-MM-DD 날짜
 * 작성 날짜: 2026/08/12
 */
function shift_date(date: LocalDateString, day_offset: number): LocalDateString {
    const date_value = new Date(`${date}T00:00:00.000Z`);

    date_value.setUTCDate(date_value.getUTCDate() + day_offset);

    return date_value.toISOString().slice(0, 10);
}

/**
 * 함수 이름: is_valid_file_name()
 * 기능: CSV 파일명에 공백만 있거나 파일 시스템 금지 문자가 포함되었는지 검사한다.
 * 인자: file_name -> 검사할 파일명
 * 반환값: 파일명이 유효하면 true
 * 작성 날짜: 2026/08/12
 */
function is_valid_file_name(file_name: string): boolean {
    return file_name.trim().length > 0 && !/[<>:"/\\|?*\u0000-\u001F]/u.test(file_name);
}

/**
 * 함수 이름: normalize_file_name()
 * 기능: 사용자가 입력한 파일명의 공백을 제거하고 CSV 확장자를 보장한다.
 * 인자: file_name -> 사용자가 입력한 파일명
 * 반환값: .csv 확장자가 포함된 파일명
 * 작성 날짜: 2026/08/12
 */
function normalize_file_name(file_name: string): string {
    const trimmed_file_name = file_name.trim();

    return trimmed_file_name.toLowerCase().endsWith('.csv')
        ? trimmed_file_name
        : `${trimmed_file_name}.csv`;
}

/**
 * 함수 이름: validate_csv_context()
 * 기능: CSV 내보내기 draft의 경로, 파일명과 날짜 범위를 한 번에 검증한다.
 * 인자: context -> 현재 CSV draft context
 * 반환값: 필드별 검증 오류
 * 작성 날짜: 2026/08/12
 */
function validate_csv_context(context: CsvExportMachineContext): CsvValidationErrors {
    const has_complete_date_range = context.start_date !== null && context.end_date !== null;
    const is_ordered_date_range = has_complete_date_range
        && context.start_date <= context.end_date;

    return {
        directory: context.directory === null || context.directory.trim().length === 0
            ? '저장 위치를 선택해주세요.'
            : null,
        file_name: is_valid_file_name(context.file_name_draft)
            ? null
            : '유효한 파일명을 입력해주세요.',
        date_range: is_ordered_date_range
            ? null
            : '시작일은 종료일보다 늦을 수 없습니다.',
    };
}

/**
 * 함수 이름: create_csv_export_machine()
 * 기능: CSV dialog draft, 폴더 picker, 기간·달력·파일명 검증과 비동기 내보내기 상태를 생성한다.
 * 인자: command_port -> 폴더 선택과 CSV 생성 port, options -> 기준 날짜와 기본 파일명
 * 반환값: csv-export feature의 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_csv_export_machine(
    command_port: UiCommandPort,
    options: CsvExportMachineOptions,
) {
    const get_current_kst_date = options.get_current_kst_date ?? (() => options.today);
    const initial_default_file_name = options.default_file_name
        ?? `binance_trades_${options.today}.csv`;
    const empty_validation_errors: CsvValidationErrors = {
        directory: null,
        file_name: null,
        date_range: null,
    };

    return setup({
        types: {
            context: {} as CsvExportMachineContext,
            events: {} as CsvExportMachineEvent,
        },
        actors: {
            pick_directory: fromPromise<string | null>(async () => {
                return command_port.pick_csv_directory();
            }),
            export_csv: fromPromise<CsvExportReceipt, CsvExportOptions>(async ({ input }) => {
                return command_port.export_csv(input);
            }),
        },
        guards: {
            is_csv_draft_valid: ({ context }) => {
                const validation_errors = validate_csv_context(context);

                return Object.values(validation_errors).every((error) => error === null);
            },
            is_file_name_valid: ({ context }) => is_valid_file_name(context.file_name_draft),
            is_start_date_valid: ({ context, event }) => {
                return event.type === 'START_DATE_SELECTED'
                    && (context.end_date === null || event.date <= context.end_date);
            },
            is_end_date_valid: ({ context, event }) => {
                return event.type === 'FINISH_DATE_SELECTED'
                    && (context.start_date === null || context.start_date <= event.date);
            },
            is_period_weekly: ({ context }) => context.period === 'last7days',
            is_period_monthly: ({ context }) => context.period === 'last30days',
            is_period_custom: ({ context }) => context.period === 'custom',
            has_selected_directory: ({ event }) => {
                return 'output' in event
                    && typeof event.output === 'string'
                    && event.output.trim().length > 0;
            },
        },
        actions: {
            reset_draft: assign(() => {
                // 한 dialog open 경계에서 날짜를 한 번만 읽어 자정 전후 필드 drift를 막는다.
                const current_today = get_current_kst_date();
                const current_default_file_name = options.default_file_name
                    ?? `binance_trades_${current_today}.csv`;

                return {
                    today: current_today,
                    directory: null,
                    period: 'today' as const,
                    start_date: current_today,
                    end_date: current_today,
                    file_name: current_default_file_name,
                    file_name_draft: current_default_file_name,
                    file_name_committed: false,
                    calendar_target: null,
                    validation_errors: empty_validation_errors,
                    command_error: null,
                    receipt: null,
                };
            }),
            store_directory: assign({
                directory: ({ context, event }) => {
                    return 'output' in event && typeof event.output === 'string'
                        ? event.output
                        : context.directory;
                },
                validation_errors: ({ context }) => ({
                    ...context.validation_errors,
                    directory: null,
                }),
                command_error: null,
            }),
            remember_picker_failure: assign({
                command_error: ({ event }) => to_ui_command_failure(
                    'error' in event ? event.error : null,
                    'DIRECTORY_PICKER_FAILED',
                    '저장 위치 선택 창을 열지 못했습니다.',
                ),
            }),
            select_today: assign({
                period: 'today',
                start_date: ({ context }) => context.today,
                end_date: ({ context }) => context.today,
                calendar_target: null,
                validation_errors: empty_validation_errors,
            }),
            select_weekly: assign({
                period: 'last7days',
                start_date: ({ context }) => shift_date(context.today, -6),
                end_date: ({ context }) => context.today,
                calendar_target: null,
                validation_errors: empty_validation_errors,
            }),
            select_monthly: assign({
                period: 'last30days',
                start_date: ({ context }) => shift_date(context.today, -29),
                end_date: ({ context }) => context.today,
                calendar_target: null,
                validation_errors: empty_validation_errors,
            }),
            select_custom: assign({
                period: 'custom',
                calendar_target: null,
                validation_errors: empty_validation_errors,
            }),
            open_start_calendar: assign({ calendar_target: 'start_date' }),
            open_end_calendar: assign({ calendar_target: 'end_date' }),
            close_calendar: assign({ calendar_target: null }),
            store_start_date: assign({
                start_date: ({ context, event }) => {
                    return event.type === 'START_DATE_SELECTED'
                        ? event.date
                        : context.start_date;
                },
                validation_errors: ({ context }) => ({
                    ...context.validation_errors,
                    date_range: null,
                }),
            }),
            store_end_date: assign({
                end_date: ({ context, event }) => {
                    return event.type === 'FINISH_DATE_SELECTED'
                        ? event.date
                        : context.end_date;
                },
                validation_errors: ({ context }) => ({
                    ...context.validation_errors,
                    date_range: null,
                }),
            }),
            remember_invalid_start_date: assign({
                validation_errors: ({ context }) => ({
                    ...context.validation_errors,
                    date_range: '시작일은 종료일보다 늦을 수 없습니다.',
                }),
            }),
            remember_invalid_end_date: assign({
                validation_errors: ({ context }) => ({
                    ...context.validation_errors,
                    date_range: '종료일은 시작일보다 이를 수 없습니다.',
                }),
            }),
            update_file_name_draft: assign({
                file_name_draft: ({ context, event }) => {
                    return event.type === 'FILE_NAME_CHANGED'
                        ? event.file_name
                        : context.file_name_draft;
                },
            }),
            commit_file_name: assign({
                file_name_committed: true,
                file_name: ({ context }) => normalize_file_name(context.file_name_draft),
                file_name_draft: ({ context }) => normalize_file_name(context.file_name_draft),
                validation_errors: ({ context }) => ({
                    ...context.validation_errors,
                    file_name: null,
                }),
            }),
            remember_invalid_file_name: assign({
                validation_errors: ({ context }) => ({
                    ...context.validation_errors,
                    file_name: '빈 파일명과 파일 시스템 금지 문자는 사용할 수 없습니다.',
                }),
            }),
            validate_draft: assign({
                validation_errors: ({ context }) => validate_csv_context(context),
            }),
            store_receipt: assign({
                receipt: ({ event }) => {
                    return 'output' in event
                        ? event.output as CsvExportReceipt
                        : null;
                },
                command_error: null,
            }),
            remember_export_failure: assign({
                command_error: ({ event }) => to_ui_command_failure(
                    'error' in event ? event.error : null,
                    'CSV_EXPORT_FAILED',
                    'CSV 파일을 생성하지 못했습니다.',
                ),
            }),
            clear_command_error: assign({ command_error: null }),
        },
    }).createMachine({
        id: 'csvExportMachine',
        initial: 'closed',
        context: {
            today: options.today,
            directory: null,
            period: 'today',
            start_date: options.today,
            end_date: options.today,
            file_name: initial_default_file_name,
            file_name_draft: initial_default_file_name,
            file_name_committed: false,
            calendar_target: null,
            validation_errors: empty_validation_errors,
            command_error: null,
            receipt: null,
        },
        states: {
            closed: {
                meta: { spec_ids: ['TD4-01', 'TD4-02'] },
                on: {
                    CSV_EXPORT_CLICKED: {
                        target: 'editing',
                        actions: 'reset_draft',
                    },
                },
            },
            editing: {
                type: 'parallel',
                meta: {
                    spec_ids: ['TD4-02', 'TD4-03', 'TD4-04', 'TD4-05', 'CR-11', 'ER-13'],
                },
                on: {
                    CLOSE_CSV_EXPORT_POPUP: { target: 'closed' },
                    CSV_DIALOG_OUTSIDE_CLICKED: { target: 'closed' },
                    EXPORT_CSV: [
                        { guard: 'is_csv_draft_valid', target: 'exporting' },
                        { actions: 'validate_draft' },
                    ],
                },
                states: {
                    file_browser: {
                        initial: 'closed',
                        states: {
                            closed: {
                                meta: { spec_ids: ['CR1-01'] },
                                on: {
                                    SAVE_LOCATION_SELECT_CLICKED: {
                                        target: 'opened',
                                    },
                                },
                            },
                            opened: {
                                meta: {
                                    spec_ids: ['CR1-02', 'CR1-03', 'CR1-04', 'CR-12'],
                                    pending: true,
                                },
                                invoke: {
                                    id: 'pick_directory_command',
                                    src: 'pick_directory',
                                    onDone: [
                                        {
                                            guard: 'has_selected_directory',
                                            target: 'closed',
                                            actions: 'store_directory',
                                        },
                                        { target: 'closed' },
                                    ],
                                    onError: {
                                        target: 'closed',
                                        actions: 'remember_picker_failure',
                                    },
                                },
                            },
                        },
                    },
                    period: {
                        initial: 'today',
                        on: {
                            SELECT_CSV_TODAY_HISTORY: {
                                target: '.today',
                                actions: 'select_today',
                            },
                            SELECT_CSV_WEEKLY_HISTORY: {
                                target: '.weekly',
                                actions: 'select_weekly',
                            },
                            SELECT_CSV_MONTHLY_HISTORY: {
                                target: '.monthly',
                                actions: 'select_monthly',
                            },
                            SELECT_CSV_DATE: {
                                target: '.custom.idle',
                                actions: 'select_custom',
                            },
                        },
                        states: {
                            today: {
                                meta: {
                                    spec_ids: ['CR2-01', 'CR2-02', 'CR2-03', 'CR2-04'],
                                },
                                always: [
                                    { guard: 'is_period_weekly', target: 'weekly' },
                                    { guard: 'is_period_monthly', target: 'monthly' },
                                    { guard: 'is_period_custom', target: 'custom.idle' },
                                ],
                            },
                            weekly: {
                                meta: {
                                    spec_ids: ['CR2-02', 'CR2-05', 'CR2-06', 'CR2-07'],
                                },
                            },
                            monthly: {
                                meta: {
                                    spec_ids: ['CR2-03', 'CR2-06', 'CR2-08', 'CR2-09', 'CR2-10'],
                                },
                            },
                            custom: {
                                initial: 'idle',
                                states: {
                                    idle: {
                                        meta: {
                                            spec_ids: ['CR2-04', 'CR2-07', 'CR2-10', 'CR2-11', 'CR2-12', 'CR2-13', 'CR2-14', 'CR2-15'],
                                        },
                                        entry: 'close_calendar',
                                        on: {
                                            START_CSV_START_DATE_SELECTION: {
                                                target: 'start_calendar',
                                                actions: 'open_start_calendar',
                                            },
                                            START_CSV_FINISH_DATE_SELECTION: {
                                                target: 'end_calendar',
                                                actions: 'open_end_calendar',
                                            },
                                        },
                                    },
                                    start_calendar: {
                                        meta: {
                                            spec_ids: ['CR2-14', 'CR2-16', 'CR2-17', 'CR2-18'],
                                        },
                                        on: {
                                            START_CSV_FINISH_DATE_SELECTION: {
                                                target: 'end_calendar',
                                                actions: 'open_end_calendar',
                                            },
                                            START_DATE_SELECTED: [
                                                { guard: 'is_start_date_valid', actions: 'store_start_date' },
                                                { actions: 'remember_invalid_start_date' },
                                            ],
                                            START_DATE_CALENDAR_OUTSIDE_CLICKED: {
                                                target: 'idle',
                                            },
                                        },
                                    },
                                    end_calendar: {
                                        meta: {
                                            spec_ids: ['CR2-15', 'CR2-19', 'CR2-20', 'CR2-21'],
                                        },
                                        on: {
                                            START_CSV_START_DATE_SELECTION: {
                                                target: 'start_calendar',
                                                actions: 'open_start_calendar',
                                            },
                                            FINISH_DATE_SELECTED: [
                                                { guard: 'is_end_date_valid', actions: 'store_end_date' },
                                                { actions: 'remember_invalid_end_date' },
                                            ],
                                            FINISH_DATE_CALENDAR_OUTSIDE_CLICKED: {
                                                target: 'idle',
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                    file_name: {
                        initial: 'committed',
                        on: {
                            FILE_NAME_CHANGED: { actions: 'update_file_name_draft' },
                        },
                        states: {
                            committed: {
                                meta: { spec_ids: ['CR3-01', 'CR3-02', 'CR3-07'] },
                                on: {
                                    FILE_NAME_CLICKED: { target: 'editing' },
                                },
                            },
                            editing: {
                                meta: { spec_ids: ['CR3-02', 'CR3-03', 'CR3-04', 'CR3-05', 'CR3-06'] },
                                on: {
                                    ENTER_KEY_TYPED: [
                                        {
                                            guard: 'is_file_name_valid',
                                            target: 'committed',
                                            actions: 'commit_file_name',
                                        },
                                        { actions: 'remember_invalid_file_name' },
                                    ],
                                    FILE_NAME_INPUT_FOCUS_LOST: [
                                        {
                                            guard: 'is_file_name_valid',
                                            target: 'committed',
                                            actions: 'commit_file_name',
                                        },
                                        { actions: 'remember_invalid_file_name' },
                                    ],
                                },
                            },
                        },
                    },
                },
            },
            exporting: {
                meta: {
                    spec_ids: ['TD4-04', 'TD4-06', 'TD4-07', 'VR-07'],
                    pending: true,
                },
                invoke: {
                    id: 'export_csv_command',
                    src: 'export_csv',
                    input: ({ context }) => ({
                        directory: context.directory as string,
                        file_name: normalize_file_name(context.file_name_draft),
                        period: context.period,
                        start_date: context.start_date as string,
                        end_date: context.end_date as string,
                        timezone: 'Asia/Seoul',
                    }),
                    onDone: {
                        target: 'complete',
                        actions: 'store_receipt',
                    },
                    onError: {
                        target: 'error',
                        actions: 'remember_export_failure',
                    },
                },
            },
            complete: {
                meta: { spec_ids: ['TD4-06', 'TD4-09'] },
                on: {
                    ACCEPT_CLOSE_ALL_POPUP: {
                        target: 'closed',
                    },
                },
            },
            error: {
                meta: { spec_ids: ['TD4-07', 'TD4-08'] },
                on: {
                    CSV_EXPORT_ERROR_CONFIRMED: {
                        target: 'editing',
                        actions: 'clear_command_error',
                    },
                },
            },
        },
    });
}
