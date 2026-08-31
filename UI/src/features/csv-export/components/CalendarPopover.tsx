import type { KeyboardEvent } from 'react';

import type { CalendarViewModel } from './types';

import styles from './CalendarPopover.module.css';

const weekday_labels = ['일', '월', '화', '수', '목', '금', '토'] as const;
const month_options = Array.from({ length: 12 }, (_, index) => index + 1);

export interface CalendarPopoverProps {
  targetLabel: string;
  calendar: CalendarViewModel;
  onDateSelect: (date: string) => void;
  onPreviousMonth: () => void;
  onNextMonth: () => void;
  onMonthChange?: (month: number) => void;
  onYearChange?: (year: number) => void;
  onDismiss: () => void;
}

/**
 * 함수 이름: pad_date_part()
 * 기능: 달력 날짜의 월과 일을 ISO 날짜 형식에 맞게 두 자리로 만든다.
 * 인자: value -> 월 또는 일 숫자
 * 반환값: 두 자리 날짜 문자열
 * 작성 날짜: 2026/08/12
 */
function pad_date_part(value: number) {
  return String(value).padStart(2, '0');
}

/**
 * 함수 이름: create_iso_date()
 * 기능: 달력의 연도, 월, 일을 시간대 영향이 없는 ISO 날짜 문자열로 조합한다.
 * 인자: year -> 연도, month -> 월, day -> 일
 * 반환값: YYYY-MM-DD 형식의 날짜 문자열
 * 작성 날짜: 2026/08/12
 */
function create_iso_date(year: number, month: number, day: number) {
  return `${year}-${pad_date_part(month)}-${pad_date_part(day)}`;
}

/**
 * 함수 이름: create_calendar_cells()
 * 기능: 표시 월의 첫 요일과 일수를 기준으로 주 단위 달력 셀을 만든다.
 * 인자: year -> 표시 연도, month -> 표시 월
 * 반환값: 빈 앞 셀과 날짜를 포함한 달력 셀 목록
 * 작성 날짜: 2026/08/12
 */
function create_calendar_cells(year: number, month: number) {
  const first_weekday = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  const day_count = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const week_count = Math.ceil((first_weekday + day_count) / 7);
  const cell_count = week_count * 7;

  return Array.from({ length: cell_count }, (_, index) => {
    const day = index - first_weekday + 1;

    return day >= 1 && day <= day_count ? day : null;
  });
}

/**
 * 함수 이름: handle_calendar_key_down()
 * 기능: Escape 입력을 달력 닫기 의도로 변환한다.
 * 인자: event -> 달력 표면의 키보드 이벤트, on_dismiss -> 닫기 의도 처리 함수
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
function handle_calendar_key_down(event: KeyboardEvent<HTMLElement>, on_dismiss: () => void) {
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    on_dismiss();
  }
}

/**
 * 함수 이름: move_calendar_grid_focus()
 * 기능: 날짜 grid의 방향키·Home·End 입력을 같은 월의 날짜 focus 이동으로 변환한다.
 * 인자: event -> 날짜 버튼 키보드 이벤트
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
function move_calendar_grid_focus(event: KeyboardEvent<HTMLButtonElement>) {
  const movement_by_key: Readonly<Record<string, number>> = {
    ArrowLeft: -1,
    ArrowRight: 1,
    ArrowUp: -7,
    ArrowDown: 7,
  };
  const grid = event.currentTarget.closest('[role="grid"]');
  const enabled_days = grid === null
    ? []
    : Array.from(grid.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'));
  const current_index = enabled_days.indexOf(event.currentTarget);

  if (current_index < 0) {
    return;
  }

  let target_index: number | null = null;

  if (event.key === 'Home') {
    target_index = 0;
  } else if (event.key === 'End') {
    target_index = enabled_days.length - 1;
  } else {
    const movement = movement_by_key[event.key];

    if (movement !== undefined) {
      target_index = current_index + movement;
    }
  }

  if (target_index === null) {
    return;
  }

  event.preventDefault();
  enabled_days[Math.min(enabled_days.length - 1, Math.max(0, target_index))]?.focus();
}

/**
 * 함수 이름: CalendarPopover()
 * 기능: CSV 사용자 지정 기간의 시작일 또는 종료일을 선택하는 제어형 월 달력을 표시한다.
 * 인자: props -> 선택 대상, 표시 월, 선택 날짜와 달력 사용자 의도 처리 함수
 * 반환값: 날짜 선택 달력 팝오버
 * 작성 날짜: 2026/08/12
 */
export function CalendarPopover({
  targetLabel,
  calendar,
  onDateSelect,
  onPreviousMonth,
  onNextMonth,
  onMonthChange,
  onYearChange,
  onDismiss,
}: CalendarPopoverProps) {
  const calendar_cells = create_calendar_cells(calendar.year, calendar.month);
  const calendar_rows = Array.from(
    { length: calendar_cells.length / weekday_labels.length },
    (_, row_index) => calendar_cells.slice(
      row_index * weekday_labels.length,
      (row_index + 1) * weekday_labels.length,
    ),
  );
  const disabled_dates = new Set(calendar.disabledDates ?? []);
  const year_options = Array.from({ length: 7 }, (_, index) => calendar.year - 3 + index);

  return (
    <section
      aria-label={`${targetLabel} 달력`}
      className={styles.popover}
      data-calendar-popover="true"
      onKeyDown={(event) => handle_calendar_key_down(event, onDismiss)}
      role="dialog"
      tabIndex={-1}
    >
      {/* 팝오버 제목은 페이지 전역 banner와 충돌하지 않는 일반 container로 유지한다. */}
      <div className={styles.header}>
        <span aria-hidden="true" className={styles.calendarIcon}>
          ▦
        </span>
        <strong>{targetLabel}</strong>
        <span className={styles.selectedDate}>
          {(calendar.headerDate ?? calendar.selectedDate).replaceAll('-', '.')}
        </span>
      </div>

      <nav aria-label="달력 월 이동" className={styles.navigation}>
        <button aria-label="이전 달" onClick={onPreviousMonth} type="button">
          ‹
        </button>
        <select
          aria-label="달력 연도"
          onChange={(event) => onYearChange?.(Number(event.target.value))}
          value={calendar.year}
        >
          {year_options.map((year) => <option key={year} value={year}>{year}년</option>)}
        </select>
        <select
          aria-label="달력 월"
          onChange={(event) => onMonthChange?.(Number(event.target.value))}
          value={calendar.month}
        >
          {month_options.map((month) => <option key={month} value={month}>{month}월</option>)}
        </select>
        <button aria-label="다음 달" onClick={onNextMonth} type="button">
          ›
        </button>
      </nav>

      <div aria-hidden="true" className={styles.weekdays}>
        {weekday_labels.map((weekday) => (
          <span key={weekday}>{weekday}</span>
        ))}
      </div>

      <div
        aria-label={`${calendar.year}년 ${calendar.month}월 날짜`}
        className={styles.days}
        role="grid"
      >
        {calendar_rows.map((calendar_row, row_index) => (
          <div className={styles.week} key={`week-${row_index}`} role="row">
            {calendar_row.map((day, column_index) => {
              const cell_index = row_index * weekday_labels.length + column_index;
              if (day === null) {
                // 빈 칸도 accessibility tree에 남겨 각 row의 일곱 요일 column 위치를 보존한다.
                return (
                  <span
                    aria-disabled="true"
                    key={`empty-${cell_index}`}
                    role="gridcell"
                  />
                );
              }

              const iso_date = create_iso_date(calendar.year, calendar.month, day);
              const is_selected = iso_date === calendar.selectedDate;
              const is_disabled = disabled_dates.has(iso_date);
              const weekend_class = column_index === 0
                ? styles.sunday
                : column_index === 6
                  ? styles.saturday
                  : '';

              return (
                <button
                  aria-label={`${calendar.year}년 ${calendar.month}월 ${day}일 선택`}
                  aria-selected={is_selected}
                  className={`${weekend_class} ${is_selected ? styles.selected : ''}`}
                  disabled={is_disabled}
                  key={iso_date}
                  onClick={() => onDateSelect(iso_date)}
                  onKeyDown={move_calendar_grid_focus}
                  role="gridcell"
                  type="button"
                >
                  {day}
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </section>
  );
}
