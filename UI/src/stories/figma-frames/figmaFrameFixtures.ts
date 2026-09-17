// figmaFrameFixtures에서 재사용할 고정 설정과 테스트 데이터를 제공한다.

import type {
  CSVExportDraftViewModel,
  CSVExportValidationErrors,
  CalendarTarget,
  CalendarViewModel,
} from '../../features/csv-export';
import type { TradingDialogKind } from '../../features/trading-control/components/TradingConfirmationDialog';

export const FIGMA_FRAME_KEYS = [
  'realtime-indicator',
  'recent-orders',
  'indicator-settings',
  'trade-history',
  'start-confirmation',
  'stop-with-position',
  'stop-without-position',
  'csv-end-calendar',
  'csv-start-calendar',
  'csv-dialog',
  'regime-confirmation',
  'regime-required',
  'regime-highlight',
  'history-empty',
  'start-loading',
  'csv-validation-error',
] as const;

export type FigmaFrameKey = (typeof FIGMA_FRAME_KEYS)[number];

export interface CsvStoryFixture {
  readonly calendar: CalendarViewModel | null;
  readonly calendar_target: CalendarTarget | null;
  readonly draft: CSVExportDraftViewModel;
  readonly errors: CSVExportValidationErrors | null;
}

export interface FigmaFrameFixture {
  readonly frame_number: number;
  readonly reference_file: string;
  readonly route: 'dashboard' | 'trade_history';
  readonly trader_tab: 'recent' | 'realtime';
  readonly trading_dialog: TradingDialogKind | null;
  readonly show_indicator_settings: boolean;
  readonly show_regime_confirmation: boolean;
  readonly highlight_regime: boolean;
  readonly regime_is_selected: boolean;
  readonly history_is_empty: boolean;
  readonly csv_export: CsvStoryFixture | null;
  readonly header: {
    readonly is_connected: boolean;
    readonly is_trading: boolean;
    readonly is_command_pending: boolean;
  };
}

const history_csv_draft: CSVExportDraftViewModel = {
  saveLocation: '~/Downloads/binance-trades',
  period: 'CUSTOM',
  startDate: '2026-06-22',
  endDate: '2026-06-29',
  fileName: 'ETH_trade_history_260629.csv',
};

const default_header = {
  is_connected: true,
  is_trading: true,
  is_command_pending: false,
} as const;

const dashboard_defaults = {
  route: 'dashboard',
  trader_tab: 'recent',
  trading_dialog: null,
  show_indicator_settings: false,
  show_regime_confirmation: false,
  highlight_regime: false,
  regime_is_selected: true,
  history_is_empty: false,
  csv_export: null,
  header: default_header,
} as const;

const history_defaults = {
  route: 'trade_history',
  trader_tab: 'recent',
  trading_dialog: null,
  show_indicator_settings: false,
  show_regime_confirmation: false,
  highlight_regime: false,
  regime_is_selected: true,
  history_is_empty: false,
  csv_export: null,
  header: default_header,
} as const;

export const FIGMA_FRAME_FIXTURES: Readonly<Record<FigmaFrameKey, FigmaFrameFixture>> = {
  'realtime-indicator': {
    ...dashboard_defaults,
    frame_number: 1,
    reference_file: '01-realtime-indicator.png',
    trader_tab: 'realtime',
  },
  'recent-orders': {
    ...dashboard_defaults,
    frame_number: 2,
    reference_file: '02-recent-orders.png',
  },
  'indicator-settings': {
    ...dashboard_defaults,
    frame_number: 3,
    reference_file: '03-indicator-settings.png',
    show_indicator_settings: true,
  },
  'trade-history': {
    ...history_defaults,
    frame_number: 4,
    reference_file: '04-trade-history.png',
  },
  'start-confirmation': {
    ...dashboard_defaults,
    frame_number: 5,
    reference_file: '05-start-confirm.png',
    trading_dialog: 'start',
    header: {
      ...default_header,
      is_trading: false,
    },
  },
  'stop-with-position': {
    ...dashboard_defaults,
    frame_number: 6,
    reference_file: '06-stop-with-position.png',
    trading_dialog: 'forceStop',
  },
  'stop-without-position': {
    ...dashboard_defaults,
    frame_number: 7,
    reference_file: '07-stop-no-position.png',
    trading_dialog: 'stop',
  },
  'csv-end-calendar': {
    ...history_defaults,
    frame_number: 8,
    reference_file: '08-csv-end-calendar.png',
    csv_export: {
      draft: history_csv_draft,
      calendar_target: 'END',
      calendar: {
        year: 2026,
        month: 6,
        selectedDate: '2026-06-29',
      },
      errors: null,
    },
  },
  'csv-start-calendar': {
    ...history_defaults,
    frame_number: 9,
    reference_file: '09-csv-start-calendar.png',
    csv_export: {
      draft: {
        ...history_csv_draft,
        startDate: '2026-05-09',
      },
      calendar_target: 'START',
      calendar: {
        year: 2026,
        month: 5,
        selectedDate: '2026-05-09',
      },
      errors: null,
    },
  },
  'csv-dialog': {
    ...history_defaults,
    frame_number: 10,
    reference_file: '10-csv-no-calendar.png',
    csv_export: {
      draft: history_csv_draft,
      calendar_target: null,
      calendar: null,
      errors: null,
    },
  },
  'regime-confirmation': {
    ...dashboard_defaults,
    frame_number: 11,
    reference_file: '11-regime-confirm.png',
    show_regime_confirmation: true,
  },
  'regime-required': {
    ...dashboard_defaults,
    frame_number: 12,
    reference_file: '12-regime-required.png',
    trading_dialog: 'regimeRequired',
    regime_is_selected: false,
    header: {
      ...default_header,
      is_trading: false,
    },
  },
  'regime-highlight': {
    ...dashboard_defaults,
    frame_number: 13,
    reference_file: '13-regime-highlight.png',
    highlight_regime: true,
    regime_is_selected: false,
    header: {
      ...default_header,
      is_trading: false,
    },
  },
  'history-empty': {
    ...history_defaults,
    frame_number: 14,
    reference_file: '14-history-empty.png',
    history_is_empty: true,
  },
  'start-loading': {
    ...dashboard_defaults,
    frame_number: 15,
    reference_file: '15-start-loading.png',
    trading_dialog: 'starting',
    header: {
      ...default_header,
      is_trading: false,
      is_command_pending: true,
    },
  },
  'csv-validation-error': {
    ...history_defaults,
    frame_number: 16,
    reference_file: '16-csv-error.png',
    csv_export: {
      draft: {
        ...history_csv_draft,
        saveLocation: null,
      },
      calendar_target: 'END',
      calendar: {
        year: 2026,
        month: 6,
        selectedDate: '2026-06-29',
        headerDate: '2026-06-22',
      },
      errors: {
        saveLocation: '저장 위치를 선택해야 합니다',
        dateRange: '시작일이 종료일보다 늦을 수 없습니다',
      },
    },
  },
};
