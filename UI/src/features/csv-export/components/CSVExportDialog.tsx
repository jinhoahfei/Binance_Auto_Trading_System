import * as Dialog from '@radix-ui/react-dialog';

import csv_export_icon_bg from '../../../assets/figma/csv-export-icon-bg.svg';
import { use_dialog_focus_return } from '../../../shared/hooks';
import { CalendarPopover } from './CalendarPopover';
import type {
  CSVExportDraftViewModel,
  CSVExportPeriod,
  CSVExportValidationErrors,
  CalendarTarget,
  CalendarViewModel,
} from './types';

import styles from './CSVExportDialog.module.css';

const period_options: ReadonlyArray<{ value: CSVExportPeriod; label: string }> = [
  { value: 'TODAY', label: '오늘' },
  { value: 'WEEKLY', label: '최근 7일' },
  { value: 'MONTHLY', label: '최근 30일' },
  { value: 'CUSTOM', label: '날짜 선택' },
];

export const DEFAULT_CSV_EXPORT_DRAFT: CSVExportDraftViewModel = {
  saveLocation: '~/Downloads/binance-trades',
  period: 'TODAY',
  startDate: '2026-08-12',
  endDate: '2026-08-12',
  fileName: 'ETH_trade_history_260812.csv',
};

export interface CSVExportDialogProps {
  open: boolean;
  draft: CSVExportDraftViewModel;
  errors?: CSVExportValidationErrors;
  calendarTarget?: CalendarTarget | null;
  calendar?: CalendarViewModel;
  exporting?: boolean;
  onDismiss: () => void;
  onOutsideDismiss?: () => void;
  onChooseLocation: () => void;
  onPeriodChange: (period: CSVExportPeriod) => void;
  onCalendarOpen: (target: CalendarTarget) => void;
  onCalendarDismiss: () => void;
  onCalendarDateSelect: (target: CalendarTarget, date: string) => void;
  onCalendarPreviousMonth: (target: CalendarTarget) => void;
  onCalendarNextMonth: (target: CalendarTarget) => void;
  onCalendarMonthChange?: (target: CalendarTarget, month: number) => void;
  onCalendarYearChange?: (target: CalendarTarget, year: number) => void;
  onFileNameChange: (file_name: string) => void;
  onFileNameEditStart?: () => void;
  onFileNameCommit?: () => void;
  onExport: () => void;
}

/**
 * 함수 이름: CSVExportDialog()
 * 기능: 저장 위치, 기간, 날짜와 파일명을 입력받는 제어형 CSV 내보내기 모달을 표시한다.
 * 인자: props -> 팝업 상태, CSV 임시 옵션, 검증 오류와 모든 사용자 의도 처리 함수
 * 반환값: CSV 내보내기 모달과 선택적으로 열린 달력 팝오버
 * 작성 날짜: 2026/08/12
 */
export function CSVExportDialog({
  open,
  draft,
  errors = {},
  calendarTarget = null,
  calendar,
  exporting = false,
  onDismiss,
  onOutsideDismiss,
  onChooseLocation,
  onPeriodChange,
  onCalendarOpen,
  onCalendarDismiss,
  onCalendarDateSelect,
  onCalendarPreviousMonth,
  onCalendarNextMonth,
  onCalendarMonthChange,
  onCalendarYearChange,
  onFileNameChange,
  onFileNameEditStart,
  onFileNameCommit,
  onExport,
}: CSVExportDialogProps) {
  use_dialog_focus_return();

  if (!open) {
    return null;
  }

  const is_custom_period = draft.period === 'CUSTOM';
  const active_calendar_target = calendarTarget && calendar ? calendarTarget : null;
  const path_error_id = errors.saveLocation ? 'csv-save-location-error' : undefined;
  const date_error_id = errors.dateRange ? 'csv-date-range-error' : undefined;
  const file_name_error_id = errors.fileName ? 'csv-file-name-error' : undefined;

  return (
    <Dialog.Root open={open}>
      <Dialog.Portal>
        <Dialog.Overlay
          className={styles.overlay}
          data-testid="csv-export-overlay"
          onMouseDown={() => {
            if (active_calendar_target) {
              onCalendarDismiss();
            } else {
              (onOutsideDismiss ?? onDismiss)();
            }
          }}
        />
        <Dialog.Content
          className={`${styles.dialog} ${errors.saveLocation || errors.dateRange ? styles.dialogWithErrors : ''}`}
          onEscapeKeyDown={(event) => {
            event.preventDefault();

            if (active_calendar_target) {
              onCalendarDismiss();
            } else {
              (onOutsideDismiss ?? onDismiss)();
            }
          }}
          onInteractOutside={(event) => {
            event.preventDefault();
          }}
          onMouseDown={(event) => {
            if (!active_calendar_target || !(event.target instanceof Element)) {
              return;
            }

            const clicked_calendar = event.target.closest('[data-calendar-popover="true"]');
            const clicked_calendar_trigger = event.target.closest('[data-calendar-trigger="true"]');

            if (!clicked_calendar && !clicked_calendar_trigger) {
              onCalendarDismiss();
            }
          }}
        >
          <header className={styles.heading}>
          <span aria-hidden="true" className={styles.icon}>
            <img alt="" src={csv_export_icon_bg} />
            <strong>CSV</strong>
          </span>
          <div>
            <Dialog.Title asChild>
              <h2>CSV 내보내기</h2>
            </Dialog.Title>
            <Dialog.Description asChild>
              <p>저장 위치와 날짜 범위를 선택한 뒤 CSV 파일로 내보냅니다.</p>
            </Dialog.Description>
          </div>
          </header>

        <section className={styles.section}>
          <h3>1. 저장 위치</h3>
          <div className={styles.locationRow}>
            <div
              aria-describedby={path_error_id}
              className={`${styles.locationField} ${errors.saveLocation ? styles.invalid : ''}`}
            >
              <span>{draft.saveLocation ? '선택된 폴더' : '저장 위치를 선택해 주세요'}</span>
              <strong>{draft.saveLocation ?? '\u200b'}</strong>
            </div>
            <button className={styles.chooseButton} disabled={exporting} onClick={onChooseLocation} type="button">
              위치 선택
            </button>
          </div>
          {errors.saveLocation ? (
            <p className={styles.error} id={path_error_id} role="alert">
              ⚠ {errors.saveLocation}
            </p>
          ) : null}
        </section>

        <section className={styles.section}>
          <h3>2. 내보낼 기간</h3>
          <div aria-label="CSV 내보낼 기간" className={styles.periods} role="group">
            {period_options.map((option) => (
              <button
                aria-pressed={draft.period === option.value}
                className={draft.period === option.value ? styles.selectedPeriod : ''}
                disabled={exporting}
                key={option.value}
                onClick={() => onPeriodChange(option.value)}
                type="button"
              >
                {option.label}
              </button>
            ))}
          </div>
          {errors.dateRange ? (
            <p className={styles.error} id={date_error_id} role="alert">
              ⚠ {errors.dateRange}
            </p>
          ) : null}
        </section>

        <div className={styles.dateRow}>
          <div className={styles.dateAnchor}>
            <button
              aria-describedby={date_error_id}
              aria-expanded={active_calendar_target === 'START'}
              className={`${styles.dateField} ${is_custom_period ? styles.dateEnabled : ''} ${
                active_calendar_target === 'START' ? styles.dateOpen : ''
              } ${errors.dateRange ? styles.invalid : ''}`}
              data-calendar-trigger="true"
              disabled={!is_custom_period || exporting}
              onClick={() => onCalendarOpen('START')}
              type="button"
            >
              <span>시작일 선택</span>
              <strong>{draft.startDate.replaceAll('-', '.')}</strong>
              {is_custom_period ? <span aria-hidden="true" className={styles.dateIcon}>▦</span> : null}
            </button>
            {active_calendar_target === 'START' && calendar ? (
              <div className={styles.startCalendar}>
                <CalendarPopover
                  calendar={calendar}
                  onDateSelect={(date) => onCalendarDateSelect('START', date)}
                  onDismiss={onCalendarDismiss}
                  onNextMonth={() => onCalendarNextMonth('START')}
                  onPreviousMonth={() => onCalendarPreviousMonth('START')}
                  onMonthChange={(month) => onCalendarMonthChange?.('START', month)}
                  onYearChange={(year) => onCalendarYearChange?.('START', year)}
                  targetLabel="시작일 선택"
                />
              </div>
            ) : null}
          </div>

          <div className={styles.dateAnchor}>
            <button
              aria-describedby={date_error_id}
              aria-expanded={active_calendar_target === 'END'}
              className={`${styles.dateField} ${is_custom_period ? styles.dateEnabled : ''} ${
                active_calendar_target === 'END' ? styles.dateOpen : ''
              } ${errors.dateRange ? styles.invalid : ''}`}
              data-calendar-trigger="true"
              disabled={!is_custom_period || exporting}
              onClick={() => onCalendarOpen('END')}
              type="button"
            >
              <span>종료일 선택</span>
              <strong>{draft.endDate.replaceAll('-', '.')}</strong>
              {is_custom_period ? <span aria-hidden="true" className={styles.dateIcon}>▦</span> : null}
            </button>
            {active_calendar_target === 'END' && calendar ? (
              <div className={styles.endCalendar}>
                <CalendarPopover
                  calendar={calendar}
                  onDateSelect={(date) => onCalendarDateSelect('END', date)}
                  onDismiss={onCalendarDismiss}
                  onNextMonth={() => onCalendarNextMonth('END')}
                  onPreviousMonth={() => onCalendarPreviousMonth('END')}
                  onMonthChange={(month) => onCalendarMonthChange?.('END', month)}
                  onYearChange={(year) => onCalendarYearChange?.('END', year)}
                  targetLabel="종료일 선택"
                />
              </div>
            ) : null}
          </div>
        </div>

        <footer className={styles.footer}>
          <div className={styles.fileNameField}>
            <label htmlFor="csv-export-file-name">파일 이름</label>
            <input
              aria-describedby={file_name_error_id}
              aria-invalid={Boolean(errors.fileName)}
              disabled={exporting}
              id="csv-export-file-name"
              onBlur={onFileNameCommit}
              onChange={(event) => onFileNameChange(event.target.value)}
              onFocus={onFileNameEditStart}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  onFileNameCommit?.();
                }
              }}
              value={draft.fileName}
            />
          </div>
          <button className={styles.cancelButton} disabled={exporting} onClick={onDismiss} type="button">
            취소
          </button>
          <button className={styles.exportButton} disabled={exporting} onClick={onExport} type="button">
            {exporting ? '내보내는 중' : '내보내기'}
          </button>
        </footer>
        {errors.fileName ? (
          <p className={`${styles.error} ${styles.fileError}`} id={file_name_error_id} role="alert">
            ⚠ {errors.fileName}
          </p>
        ) : null}
        {errors.export ? (
          <p className={`${styles.error} ${styles.exportError}`} role="alert">
            ⚠ {errors.export}
          </p>
        ) : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
