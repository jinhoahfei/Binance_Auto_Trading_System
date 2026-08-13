import type { MetricTone, TradeHistorySummaryViewModel } from './types';

import styles from './SummaryCards.module.css';

export interface SummaryCardsProps {
  summary: TradeHistorySummaryViewModel;
}

/**
 * 함수 이름: get_tone_class()
 * 기능: 거래 성과의 의미에 맞는 CSS 색상 클래스를 선택한다.
 * 인자: tone -> 긍정, 부정 또는 중립 성과 색상
 * 반환값: CSS Modules 색상 클래스 이름
 * 작성 날짜: 2026/08/12
 */
function get_tone_class(tone: MetricTone) {
  if (tone === 'positive') {
    return styles.positive;
  }

  if (tone === 'negative') {
    return styles.negative;
  }

  return styles.neutral;
}

/**
 * 함수 이름: SummaryCards()
 * 기능: 당일 수익률, 매도 성과, ETH 보유량과 수수료 요약을 네 개 카드로 표시한다.
 * 인자: props -> 거래 내역 요약 표시 모델
 * 반환값: 거래 내역 상단 요약 카드 묶음
 * 작성 날짜: 2026/08/12
 */
export function SummaryCards({ summary }: SummaryCardsProps) {
  const daily_return_tone = get_tone_class(summary.dailyReturn.tone);
  const sell_performance_tone = get_tone_class(summary.sellPerformance.tone);

  return (
    <section aria-label="거래 요약" className={styles.grid}>
      <article className={styles.card}>
        <h2 className={styles.title}>
          <span className={`${styles.accent} ${styles.dailyAccent}`} />
          당일 전체 수익률
        </h2>
        <p className={`${styles.dailyValue} ${daily_return_tone}`}>{summary.dailyReturn.value}</p>
      </article>

      <article className={`${styles.card} ${styles.sellCard}`}>
        <h2 className={styles.title}>
          <span className={`${styles.accent} ${styles.sellAccent}`} />
          매도 성과
        </h2>
        <div className={styles.sellHeadline}>
          <p className={`${styles.sellRate} ${sell_performance_tone}`}>{summary.sellPerformance.winRate}</p>
          <p className={styles.sellCount}>{summary.sellPerformance.completedCount}</p>
        </div>
        <div className={styles.sellDetails}>
          <span>평균 실현수익률</span>
          <strong className={sell_performance_tone}>{summary.sellPerformance.averageRealizedReturn}</strong>
          <span>총 실현손익</span>
          <strong className={sell_performance_tone}>{summary.sellPerformance.totalRealizedPnl}</strong>
        </div>
      </article>

      <article className={styles.card}>
        <h2 className={styles.title}>
          <span className={`${styles.accent} ${styles.positionAccent}`} />
          현재 ETH 보유 수량
        </h2>
        <p className={styles.positionValue}>{summary.position.quantity}</p>
      </article>

      <article className={styles.card}>
        <h2 className={styles.title}>
          <span className={`${styles.accent} ${styles.feeAccent}`} />
          당일 수수료
        </h2>
        <p className={styles.feeValue}>{summary.fees.amount}</p>
        <p className={styles.feeHelper}>
          총 체결금액 {summary.fees.totalExecutedAmount} · 평균 슬리피지 {summary.fees.averageSlippage}
        </p>
      </article>
    </section>
  );
}
