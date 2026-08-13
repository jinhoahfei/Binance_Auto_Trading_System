import type { EmptyTradeHistoryViewModel, TradeRowViewModel } from './types';

import styles from './TradeTable.module.css';

const column_labels = [
  '시간',
  '구분',
  'REGIME',
  '전략',
  '진입당시 ETH가격',
  '체결가',
  'ETH 수량',
  '주문금액',
  '수수료',
  '직전 매수 수익률',
  '실현손익',
] as const;

export const DEFAULT_EMPTY_TRADE_HISTORY: EmptyTradeHistoryViewModel = {
  title: '거래 내역이 없습니다',
  description: '자동매매가 시작되면 거래 내역이 이곳에 표시됩니다.',
  suggestion: '날짜 필터를 변경하거나 새로운 매매를 시작해 보세요.',
  actionLabel: '자동매매 시작하기',
};

export interface TradeTableProps {
  rows: ReadonlyArray<TradeRowViewModel>;
  emptyState?: EmptyTradeHistoryViewModel;
  onStartTrading?: () => void;
}

/**
 * 함수 이름: get_result_tone_class()
 * 기능: 수익률 또는 실현손익 문자열의 부호에 맞는 색상 클래스를 선택한다.
 * 인자: value -> 화면에 표시할 수익 결과 문자열
 * 반환값: 양수, 음수 또는 중립 CSS 클래스 이름
 * 작성 날짜: 2026/08/12
 */
function get_result_tone_class(value: string) {
  if (value.trim().startsWith('+')) {
    return styles.positive;
  }

  if (value.trim().startsWith('-') && value.trim() !== '-') {
    return styles.negative;
  }

  return styles.muted;
}

/**
 * 함수 이름: TradeTable()
 * 기능: 체결 거래의 열 의미를 유지하는 스크롤 표를 표시하고 결과가 없으면 빈 상태를 표시한다.
 * 인자: props -> 거래 행, 빈 상태 문구와 자동매매 시작 의도 처리 함수
 * 반환값: 거래 내역 표 카드
 * 작성 날짜: 2026/08/12
 */
export function TradeTable({
  rows,
  emptyState = DEFAULT_EMPTY_TRADE_HISTORY,
  onStartTrading,
}: TradeTableProps) {
  return (
    <section aria-label="거래 내역 표" className={styles.card}>
      <div className={styles.scrollArea} tabIndex={0}>
        <table className={styles.table}>
          <colgroup>
            <col className={styles.timeColumn} />
            <col className={styles.sideColumn} />
            <col className={styles.regimeColumn} />
            <col className={styles.strategyColumn} />
            <col className={styles.entryColumn} />
            <col className={styles.executionColumn} />
            <col className={styles.quantityColumn} />
            <col className={styles.amountColumn} />
            <col className={styles.feeColumn} />
            <col className={styles.returnColumn} />
            <col className={styles.pnlColumn} />
          </colgroup>
          <thead>
            <tr>
              {column_labels.map((label) => (
                <th key={label} scope="col">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr className={styles.emptyRow}>
                <td colSpan={column_labels.length}>
                  <div className={styles.emptyContent}>
                    <strong>{emptyState.title}</strong>
                    <p>{emptyState.description}</p>
                    <p>{emptyState.suggestion}</p>
                    {onStartTrading ? (
                      <button onClick={onStartTrading} type="button">
                        {emptyState.actionLabel}
                      </button>
                    ) : null}
                  </div>
                </td>
              </tr>
            ) : (
              rows.map((row) => {
                const side_class = row.side === 'BUY' ? styles.buy : styles.sell;

                return (
                  <tr key={row.id}>
                    <td className={styles.time}>{row.time}</td>
                    <td>
                      <span className={`${styles.sideValue} ${side_class}`}>
                        <span aria-hidden="true" className={styles.sideDot} />
                        {row.side === 'BUY' ? '매수' : '매도'}
                      </span>
                    </td>
                    <td className={styles.regime}>{row.regime}</td>
                    <td className={styles.strategy}>{row.strategy}</td>
                    <td className={styles.numeric}>{row.entryPrice}</td>
                    <td className={`${styles.numeric} ${styles.emphasized}`}>{row.executionPrice}</td>
                    <td className={styles.numeric}>{row.quantity}</td>
                    <td className={styles.numeric}>{row.orderAmount}</td>
                    <td className={`${styles.numeric} ${styles.secondary}`}>{row.fee}</td>
                    <td className={`${styles.numeric} ${get_result_tone_class(row.previousBuyReturn)}`}>
                      {row.previousBuyReturn}
                    </td>
                    <td className={`${styles.numeric} ${get_result_tone_class(row.realizedPnl)}`}>{row.realizedPnl}</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
