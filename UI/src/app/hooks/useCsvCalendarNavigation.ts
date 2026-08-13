import { useCallback, useState } from 'react';

import type {
    CalendarTarget,
    CalendarViewModel,
} from '../../features/csv-export';

interface VisibleCalendarMonth {
    readonly year: number;
    readonly month: number;
}

interface VisibleCalendarMonths {
    readonly START: VisibleCalendarMonth;
    readonly END: VisibleCalendarMonth;
}

/**
 * CSV 달력의 표시 월 이동과 actor가 소유하는 선택일을 결합하는 결과이다.
 */
export interface CsvCalendarNavigation {
    create_calendar(target: CalendarTarget, selected_date: string): CalendarViewModel;
    prepare_calendar(target: CalendarTarget, selected_date: string): void;
    select_month(target: CalendarTarget, month: number): void;
    select_year(target: CalendarTarget, year: number): void;
    show_next_month(target: CalendarTarget): void;
    show_previous_month(target: CalendarTarget): void;
}

/**
 * 함수 이름: get_month_from_date()
 * 기능: YYYY-MM-DD 선택일에서 달력이 표시할 연도와 월을 추출한다.
 * 인자: selected_date -> 로컬 날짜 문자열
 * 반환값: 달력 표시 연도와 1부터 시작하는 월
 * 작성 날짜: 2026/08/12
 */
function get_month_from_date(selected_date: string): VisibleCalendarMonth {
    const [year_text = '2026', month_text = '1'] = selected_date.split('-');

    return {
        year: Number(year_text),
        month: Number(month_text),
    };
}

/**
 * 함수 이름: shift_calendar_month()
 * 기능: 달력 표시 연월을 이전 또는 다음 달로 안전하게 이동한다.
 * 인자: visible_month -> 현재 연월, month_offset -> 이동할 월 수
 * 반환값: 이동된 연도와 월
 * 작성 날짜: 2026/08/12
 */
function shift_calendar_month(
    visible_month: VisibleCalendarMonth,
    month_offset: number,
): VisibleCalendarMonth {
    const shifted_date = new Date(Date.UTC(
        visible_month.year,
        visible_month.month - 1 + month_offset,
        1,
    ));

    return {
        year: shifted_date.getUTCFullYear(),
        month: shifted_date.getUTCMonth() + 1,
    };
}

/**
 * 함수 이름: use_csv_calendar_navigation()
 * 기능: CSV 시작일·종료일 달력의 표시 월만 React local state로 관리한다.
 * 인자: 없음
 * 반환값: 달력 ViewModel 생성과 월 이동 함수
 * 작성 날짜: 2026/08/12
 */
export function use_csv_calendar_navigation(): CsvCalendarNavigation {
    const [visible_months, set_visible_months] = useState<VisibleCalendarMonths>({
        START: { year: 2026, month: 8 },
        END: { year: 2026, month: 8 },
    });

    const prepare_calendar = useCallback((target: CalendarTarget, selected_date: string) => {
        set_visible_months((current_months) => ({
            ...current_months,
            [target]: get_month_from_date(selected_date),
        }));
    }, []);

    const show_previous_month = useCallback((target: CalendarTarget) => {
        set_visible_months((current_months) => ({
            ...current_months,
            [target]: shift_calendar_month(current_months[target], -1),
        }));
    }, []);

    const show_next_month = useCallback((target: CalendarTarget) => {
        set_visible_months((current_months) => ({
            ...current_months,
            [target]: shift_calendar_month(current_months[target], 1),
        }));
    }, []);

    const select_year = useCallback((target: CalendarTarget, year: number) => {
        set_visible_months((current_months) => ({
            ...current_months,
            [target]: {
                ...current_months[target],
                year,
            },
        }));
    }, []);

    const select_month = useCallback((target: CalendarTarget, month: number) => {
        set_visible_months((current_months) => ({
            ...current_months,
            [target]: {
                ...current_months[target],
                month,
            },
        }));
    }, []);

    const create_calendar = useCallback((
        target: CalendarTarget,
        selected_date: string,
    ): CalendarViewModel => ({
        ...visible_months[target],
        selectedDate: selected_date,
    }), [visible_months]);

    return {
        create_calendar,
        prepare_calendar,
        select_month,
        select_year,
        show_next_month,
        show_previous_month,
    };
}
