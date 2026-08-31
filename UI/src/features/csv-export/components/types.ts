export type CSVExportPeriod = 'TODAY' | 'WEEKLY' | 'MONTHLY' | 'CUSTOM';

export type CalendarTarget = 'START' | 'END';

export interface CSVExportDraftViewModel {
  saveLocation: string | null;
  period: CSVExportPeriod;
  startDate: string;
  endDate: string;
  fileName: string;
}

export interface CSVExportValidationErrors {
  saveLocation?: string;
  dateRange?: string;
  fileName?: string;
  export?: string;
}

export interface CalendarViewModel {
  year: number;
  month: number;
  selectedDate: string;
  /** 선택 원과 별도로 달력 header에 표시할 fixture 기준 날짜다. */
  headerDate?: string;
  disabledDates?: ReadonlyArray<string>;
}
