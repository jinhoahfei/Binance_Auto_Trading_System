import {
  HistoryFilters,
  SummaryCards,
  TradeTable,
  type EmptyTradeHistoryViewModel,
  type HistoryPeriod,
  type TradeHistorySummaryViewModel,
  type TradeRowViewModel,
  type TradeSideFilter,
} from '../../features/trade-history';
import { CSVExportDialog, type CSVExportDialogProps } from '../../features/csv-export';

import styles from './TradeHistoryPage.module.css';

export interface TradeHistoryPageProps {
  title?: string;
  description: string;
  summary: TradeHistorySummaryViewModel;
  rows: ReadonlyArray<TradeRowViewModel>;
  period: HistoryPeriod;
  side: TradeSideFilter;
  isLoading?: boolean;
  filtersDisabled?: boolean;
  emptyState?: EmptyTradeHistoryViewModel;
  csvExportDialog?: CSVExportDialogProps;
  onBack: () => void;
  onPeriodChange: (period: HistoryPeriod) => void;
  onSideChange: (side: TradeSideFilter) => void;
  onExportCsv: () => void;
  onStartTrading?: () => void;
}


/**
 * 함수 이름: TradeHistoryPage()
 * 기능: 거래 내역 상세 화면의 제목, D-12 요약, 실제 조회 상태, 필터, 표와 CSV 팝업을 배치한다.
 * 인자: props -> 조회 상태 표시 모델과 상위 제어기로 전달할 사용자 의도 처리 함수
 * 반환값: 앱 헤더를 제외한 거래 내역 상세 라우트 본문
 * 작성 날짜: 2026/08/23
 */
export function TradeHistoryPage({
  title = '거래 내역 상세',
  description,
  summary,
  rows,
  period,
  side,
  isLoading = false,
  filtersDisabled = false,
  emptyState,
  csvExportDialog,
  onBack,
  onPeriodChange,
  onSideChange,
  onExportCsv,
  onStartTrading,
}: TradeHistoryPageProps) {
  // 요약·필터·표와 CSV 창을 상세 화면의 동일한 조회 상태에 연결한다.
  return (
    <main className={styles.page}>
      <header className={styles.pageHeading}>
        <button className={styles.backButton} onClick={onBack} type="button">
          <span aria-hidden="true">←</span> 돌아가기
        </button>
        <h1>{title}</h1>
        <p>{description}</p>
      </header>

      <div className={styles.summary}>
        <SummaryCards summary={summary} />
      </div>

      <div className={styles.filters}>
        <HistoryFilters
          disabled={filtersDisabled}
          onExportCsv={onExportCsv}
          onPeriodChange={onPeriodChange}
          onSideChange={onSideChange}
          period={period}
          side={side}
        />
      </div>

      <div className={styles.table}>
        <TradeTable
          isLoading={isLoading}
          rows={rows}
          {...(emptyState ? { emptyState } : {})}
          {...(onStartTrading ? { onStartTrading } : {})}
        />
      </div>

      {csvExportDialog ? <CSVExportDialog {...csvExportDialog} /> : null}
    </main>
  );
}
