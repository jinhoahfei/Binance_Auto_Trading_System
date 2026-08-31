import type { CSVExportDialogProps } from '../../features/csv-export';
import {
  EMPTY_TRADE_HISTORY_SUMMARY_FIXTURE,
  TRADE_HISTORY_ROWS_FIXTURE,
  TRADE_HISTORY_SUMMARY_FIXTURE,
} from '../../features/trade-history';
import { RegimeChangeDialog } from '../../features/regime-selection';
import { AppHeader } from '../../features/trading-control/components/AppHeader';
import { TradingConfirmationDialog } from '../../features/trading-control/components/TradingConfirmationDialog';
import {
  DashboardPage,
  DEFAULT_DASHBOARD_PROPS,
  type DashboardPageProps,
} from '../../routes/dashboard';
import { TradeHistoryPage, type TradeHistoryPageProps } from '../../routes/trade-history';

import {
  FIGMA_FRAME_FIXTURES,
  type FigmaFrameFixture,
  type FigmaFrameKey,
} from './figmaFrameFixtures';
import styles from './FigmaFrameHarness.module.css';

export interface FigmaFrameHarnessProps {
  readonly frame: FigmaFrameKey;
}

/**
 * 함수 이름: ignore_story_intent()
 * 기능: 정적 Figma Story에서 발생한 사용자 입력을 안전하게 무시한다.
 * 인자: 없음
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
function ignore_story_intent(): void {}

/**
 * 함수 이름: create_dashboard_props()
 * 기능: 공통 대시보드 fixture에 프레임별 탭, 팝오버와 REGIME 상태를 반영한다.
 * 인자: fixture -> 표시할 Figma 프레임의 결정적 상태
 * 반환값: DashboardPage에 전달할 제어형 props
 * 작성 날짜: 2026/08/12
 */
function create_dashboard_props(fixture: FigmaFrameFixture): DashboardPageProps {
  const applied_regime = fixture.show_regime_confirmation
    ? 'type2'
    : fixture.regime_is_selected
      ? DEFAULT_DASHBOARD_PROPS.regime.applied
      : null;

  return {
    account: DEFAULT_DASHBOARD_PROPS.account,
    chart: {
      ...DEFAULT_DASHBOARD_PROPS.chart,
      interval: '1m',
      indicatorSettingsOpen: fixture.show_indicator_settings,
      presentationMode: 'fixture',
      ...(fixture.show_indicator_settings
        ? {
            indicatorSettings: {
              ema9: true,
              bollingerBand: true,
              volume: false,
            },
          }
        : {}),
    },
    regime: {
      ...DEFAULT_DASHBOARD_PROPS.regime,
      applied: applied_regime,
      highlight: fixture.highlight_regime,
    },
    splitOrder: DEFAULT_DASHBOARD_PROPS.splitOrder,
    trader: {
      ...DEFAULT_DASHBOARD_PROPS.trader,
      activeTab: fixture.trader_tab,
    },
  };
}

/**
 * 함수 이름: create_csv_dialog_props()
 * 기능: CSV 관련 Figma 프레임 fixture를 제어형 CSVExportDialog props로 변환한다.
 * 인자: fixture -> CSV 팝업, 달력과 validation 상태를 포함한 프레임 fixture
 * 반환값: CSV 팝업이 없으면 undefined, 있으면 완성된 dialog props
 * 작성 날짜: 2026/08/12
 */
function create_csv_dialog_props(fixture: FigmaFrameFixture): CSVExportDialogProps | undefined {
  if (fixture.csv_export === null) {
    return undefined;
  }

  const csv_fixture = fixture.csv_export;

  return {
    open: true,
    draft: csv_fixture.draft,
    visualPeriod: 'TODAY',
    suppressDateRangeInvalidBorder: fixture.frame_number === 16,
    calendarTarget: csv_fixture.calendar_target,
    ...(csv_fixture.calendar === null ? {} : { calendar: csv_fixture.calendar }),
    ...(csv_fixture.errors === null ? {} : { errors: csv_fixture.errors }),
    onDismiss: ignore_story_intent,
    onChooseLocation: ignore_story_intent,
    onPeriodChange: ignore_story_intent,
    onCalendarOpen: ignore_story_intent,
    onCalendarDismiss: ignore_story_intent,
    onCalendarDateSelect: ignore_story_intent,
    onCalendarPreviousMonth: ignore_story_intent,
    onCalendarNextMonth: ignore_story_intent,
    onFileNameChange: ignore_story_intent,
    onExport: ignore_story_intent,
  };
}

/**
 * 함수 이름: create_trade_history_props()
 * 기능: 공통 거래 내역 fixture에 empty state와 CSV modal 상태를 결합한다.
 * 인자: fixture -> 표시할 Figma 프레임의 결정적 상태
 * 반환값: TradeHistoryPage에 전달할 제어형 props
 * 작성 날짜: 2026/08/12
 */
function create_trade_history_props(fixture: FigmaFrameFixture): TradeHistoryPageProps {
  const csv_dialog_props = create_csv_dialog_props(fixture);

  // Empty 기준도 Figma에 남아 있는 과거 매도 성과를 보존하고 당일 count만 0으로 만든다.
  const history_summary = fixture.history_is_empty
    ? {
        ...EMPTY_TRADE_HISTORY_SUMMARY_FIXTURE,
        sellPerformance: {
          ...EMPTY_TRADE_HISTORY_SUMMARY_FIXTURE.sellPerformance,
          averageRealizedReturn: TRADE_HISTORY_SUMMARY_FIXTURE.sellPerformance.averageRealizedReturn,
          completedCount: '0/0',
          totalRealizedPnl: TRADE_HISTORY_SUMMARY_FIXTURE.sellPerformance.totalRealizedPnl,
          tone: 'positive' as const,
        },
        fees: {
          ...EMPTY_TRADE_HISTORY_SUMMARY_FIXTURE.fees,
          averageSlippage: TRADE_HISTORY_SUMMARY_FIXTURE.fees.averageSlippage,
          totalExecutedAmount: TRADE_HISTORY_SUMMARY_FIXTURE.fees.totalExecutedAmount,
        },
      }
    : TRADE_HISTORY_SUMMARY_FIXTURE;

  return {
    description: '2026.06.22 · ETH/KRW · Basic Iterative · 전체 체결 6건',
    summary: history_summary,
    rows: fixture.history_is_empty ? [] : TRADE_HISTORY_ROWS_FIXTURE,
    period: 'TODAY',
    side: 'ALL',
    ...(csv_dialog_props === undefined ? {} : { csvExportDialog: csv_dialog_props }),
    onBack: ignore_story_intent,
    onPeriodChange: ignore_story_intent,
    onSideChange: ignore_story_intent,
    onExportCsv: ignore_story_intent,
    ...(fixture.history_is_empty ? { onStartTrading: ignore_story_intent } : {}),
  };
}

/**
 * 함수 이름: FigmaFrameHarness()
 * 기능: 공통 AppHeader와 route Boundary에 Figma 16개 프레임의 상태 fixture를 주입한다.
 * 인자: props -> 표시할 Figma 프레임 키
 * 반환값: 1440×1024 고정 visual regression 화면
 * 작성 날짜: 2026/08/12
 */
export function FigmaFrameHarness({ frame }: FigmaFrameHarnessProps) {
  const fixture = FIGMA_FRAME_FIXTURES[frame];
  const frame_class_name = [
    styles.frame,
    fixture.highlight_regime ? styles.frozenHighlight : '',
  ].filter(Boolean).join(' ');

  return (
    <div
      className={frame_class_name}
      data-figma-frame={frame}
      data-figma-route={fixture.route}
    >
      <AppHeader
        hasOpenPosition={false}
        isCommandPending={fixture.header.is_command_pending}
        isConnected={fixture.header.is_connected}
        isTrading={fixture.header.is_trading}
        onStartRequested={ignore_story_intent}
        onStopRequested={ignore_story_intent}
      />

      {fixture.route === 'dashboard' ? (
        <div className={styles.dashboardBody}>
          <DashboardPage {...create_dashboard_props(fixture)} />
        </div>
      ) : (
        <TradeHistoryPage {...create_trade_history_props(fixture)} />
      )}

      {fixture.trading_dialog === null ? null : (
        <TradingConfirmationDialog
          kind={fixture.trading_dialog}
          onCancel={ignore_story_intent}
          onConfirm={ignore_story_intent}
          open
          regimeLabel="Type 0(횡보)"
        />
      )}

      <RegimeChangeDialog
        onCancel={ignore_story_intent}
        onConfirm={ignore_story_intent}
        open={fixture.show_regime_confirmation}
        regimeKey="type2"
        regimeLabel="강상승"
      />
    </div>
  );
}
