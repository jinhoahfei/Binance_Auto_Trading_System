import type {
    CSVExportDialogProps,
    CSVExportPeriod,
    CalendarTarget,
} from '../../features/csv-export';
import type { AppViewModel } from '../control';
import type { CsvCalendarNavigation } from '../hooks';
import type { UiApplicationController } from '../runtime';

/**
 * 함수 이름: map_csv_period_to_view()
 * 기능: actor CSV 기간 계약을 CSVExportDialog 표시 enum으로 변환한다.
 * 인자: period -> actor CSV 기간
 * 반환값: dialog 기간 값
 * 작성 날짜: 2026/08/12
 */
function map_csv_period_to_view(
    period: AppViewModel['csv_export']['period'],
): CSVExportPeriod {
    const period_map: Readonly<Record<AppViewModel['csv_export']['period'], CSVExportPeriod>> = {
        today: 'TODAY',
        last7days: 'WEEKLY',
        last30days: 'MONTHLY',
        custom: 'CUSTOM',
    };

    return period_map[period];
}

/**
 * 함수 이름: map_view_period_to_csv()
 * 기능: CSVExportDialog 표시 enum을 actor CSV 기간 계약으로 변환한다.
 * 인자: period -> dialog에서 선택한 기간
 * 반환값: facade CSV_PERIOD_SELECTED intent 값
 * 작성 날짜: 2026/08/12
 */
function map_view_period_to_csv(
    period: CSVExportPeriod,
): AppViewModel['csv_export']['period'] {
    const period_map: Readonly<Record<CSVExportPeriod, AppViewModel['csv_export']['period']>> = {
        TODAY: 'today',
        WEEKLY: 'last7days',
        MONTHLY: 'last30days',
        CUSTOM: 'custom',
    };

    return period_map[period];
}

/**
 * 함수 이름: map_calendar_target_to_view()
 * 기능: actor 달력 target을 CSVExportDialog 표시 target으로 변환한다.
 * 인자: target -> actor 달력 target 또는 닫힘 값
 * 반환값: dialog 달력 target 또는 null
 * 작성 날짜: 2026/08/12
 */
function map_calendar_target_to_view(
    target: AppViewModel['csv_export']['calendar_target'],
): CalendarTarget | null {
    if (target === 'start_date') {
        return 'START';
    }
    if (target === 'end_date') {
        return 'END';
    }

    return null;
}

/**
 * 함수 이름: present_csv_export_dialog_props()
 * 기능: CSV actor draft와 validation 상태를 제어형 CSVExportDialog props로 투영한다.
 * 인자: view_model -> 최신 AppViewModel, controller -> UI actor controller, calendar_navigation -> local 표시 월 controller
 * 반환값: CSV 내보내기 dialog props
 * 작성 날짜: 2026/08/12
 */
export function present_csv_export_dialog_props(
    view_model: AppViewModel,
    controller: UiApplicationController,
    calendar_navigation: CsvCalendarNavigation,
): CSVExportDialogProps {
    const csv_view_model = view_model.csv_export;
    const calendar_target = map_calendar_target_to_view(csv_view_model.calendar_target);
    const start_date = csv_view_model.start_date ?? '2026-08-12';
    const end_date = csv_view_model.end_date ?? '2026-08-12';
    const selected_calendar_date = calendar_target === 'START' ? start_date : end_date;
    const validation_errors = csv_view_model.validation_errors;

    return {
        open: csv_view_model.is_open,
        draft: {
            saveLocation: csv_view_model.directory,
            period: map_csv_period_to_view(csv_view_model.period),
            startDate: start_date,
            endDate: end_date,
            fileName: csv_view_model.file_name_draft,
        },
        errors: {
            ...(validation_errors.directory === null
                ? {}
                : { saveLocation: validation_errors.directory }),
            ...(validation_errors.date_range === null
                ? {}
                : { dateRange: validation_errors.date_range }),
            ...(validation_errors.file_name === null
                ? {}
                : { fileName: validation_errors.file_name }),
            ...(csv_view_model.command_error === null
                ? {}
                : { export: csv_view_model.command_error.message }),
        },
        calendarTarget: calendar_target,
        ...(calendar_target === null
            ? {}
            : {
                calendar: calendar_navigation.create_calendar(
                    calendar_target,
                    selected_calendar_date,
                ),
            }),
        exporting: csv_view_model.is_pending,
        onDismiss: () => controller.dispatch({ type: 'CLOSE_CSV_EXPORT' }),
        onOutsideDismiss: () => controller.dispatch({ type: 'CSV_DIALOG_OUTSIDE_CLICKED' }),
        onChooseLocation: () => controller.dispatch({ type: 'CSV_DIRECTORY_SELECT_CLICKED' }),
        onPeriodChange: (period) => controller.dispatch({
            type: 'CSV_PERIOD_SELECTED',
            period: map_view_period_to_csv(period),
        }),
        onCalendarOpen: (target) => {
            const target_date = target === 'START' ? start_date : end_date;
            calendar_navigation.prepare_calendar(target, target_date);
            controller.dispatch({
                type: target === 'START'
                    ? 'CSV_START_CALENDAR_OPENED'
                    : 'CSV_END_CALENDAR_OPENED',
            });
        },
        onCalendarDismiss: () => controller.dispatch({ type: 'CSV_CALENDAR_OUTSIDE_CLICKED' }),
        onCalendarDateSelect: (target, date) => controller.dispatch({
            type: target === 'START'
                ? 'CSV_START_DATE_SELECTED'
                : 'CSV_END_DATE_SELECTED',
            date,
        }),
        onCalendarPreviousMonth: calendar_navigation.show_previous_month,
        onCalendarNextMonth: calendar_navigation.show_next_month,
        onCalendarMonthChange: calendar_navigation.select_month,
        onCalendarYearChange: calendar_navigation.select_year,
        onFileNameEditStart: () => controller.dispatch({ type: 'CSV_FILE_NAME_EDIT_STARTED' }),
        onFileNameChange: (file_name) => controller.dispatch({
            type: 'CSV_FILE_NAME_CHANGED',
            file_name,
        }),
        onFileNameCommit: () => controller.dispatch({ type: 'CSV_FILE_NAME_COMMITTED' }),
        onExport: () => {
            controller.dispatch({ type: 'CSV_FILE_NAME_EDIT_STARTED' });
            controller.dispatch({ type: 'CSV_FILE_NAME_COMMITTED' });

            if (controller.get_view_model().csv_export.validation_errors.file_name === null) {
                controller.dispatch({ type: 'CSV_EXPORT_SUBMITTED' });
            }
        },
    };
}
