import type { HistoryPeriod, TradeSide, TradeSideFilter } from './types';

import styles from './HistoryFilters.module.css';

const period_options: ReadonlyArray<{ value: HistoryPeriod; label: string }> = [
  { value: 'TODAY', label: '오늘' },
  { value: 'WEEKLY', label: '최근 7일' },
  { value: 'MONTHLY', label: '최근 30일' },
  { value: 'ALL', label: '전체' },
];

// 거래 구분은 매수·매도 두 버튼으로 표시하고, 선택 해제는 기존 ALL 필터로 전달한다.
const side_options: ReadonlyArray<{ value: TradeSide; label: string }> = [
  { value: 'BUY', label: '매수' },
  { value: 'SELL', label: '매도' },
];

export interface HistoryFiltersProps {
  period: HistoryPeriod;
  side: TradeSideFilter;
  disabled?: boolean;
  onPeriodChange: (period: HistoryPeriod) => void;
  onSideChange: (side: TradeSideFilter) => void;
  onExportCsv: () => void;
}

/**
 * 함수 이름: HistoryFilters()
 * 기능: 거래 내역의 기간 선택, 단일 거래 구분의 선택·해제와 CSV 내보내기 의도를 전달한다.
 * 인자: props -> 현재 필터, 비활성 상태와 각 사용자 의도 처리 함수
 * 반환값: 거래 내역 필터 툴바
 * 작성 날짜: 2026/08/12
 */
export function HistoryFilters({
  period,
  side,
  disabled = false,
  onPeriodChange,
  onSideChange,
  onExportCsv,
}: HistoryFiltersProps) {
  return (
    <section aria-label="거래 내역 필터" className={styles.toolbar}>
      <span className={styles.toolbarLabel}>필터</span>

      <div aria-label="기간 필터" className={styles.group} role="group">
        {period_options.map((option) => (
          <button
            aria-pressed={period === option.value}
            className={`${styles.filterButton} ${period === option.value ? styles.activePeriod : ''}`}
            disabled={disabled}
            key={option.value}
            onClick={() => onPeriodChange(option.value)}
            type="button"
          >
            {option.label}
          </button>
        ))}
      </div>

      <span aria-hidden="true" className={styles.separator} />

      <div aria-label="거래 구분 필터" className={styles.group} role="group">
        {side_options.map((option) => {
          // 상위에서 받은 단일 거래 구분으로 버튼 색상과 선택 상태를 함께 결정한다.
          const tone_class = option.value === 'BUY' ? styles.buy : styles.sell;
          const is_selected = side === option.value;
          const next_side = is_selected ? 'ALL' : option.value;  // 선택된 버튼을 다시 누르면 전체 내역을 조회한다.

          return (
            <button
              aria-pressed={is_selected}
              className={`${styles.filterButton} ${tone_class} ${is_selected ? styles.activeSide : ''}`}
              disabled={disabled}
              key={option.value}
              onClick={() => onSideChange(next_side)}
              type="button"
            >
              {option.label}
            </button>
          );
        })}
      </div>

      <button className={styles.exportButton} disabled={disabled} onClick={onExportCsv} type="button">
        CSV 내보내기
      </button>
    </section>
  );
}
