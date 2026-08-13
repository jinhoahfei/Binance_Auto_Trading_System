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
  disabledDates?: ReadonlyArray<string>;
}
