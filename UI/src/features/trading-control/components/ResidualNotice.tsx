import styles from './ResidualNotice.module.css';

export interface ResidualNoticeProps {
  quantity?: string;
  costBasis?: string;
  hasOpenPosition: boolean;
  isTrading: boolean;
}

/**
 * 함수 이름: ResidualNotice()
 * 기능: 전략 종료와 실제 잔여 ETH 보유를 구분하고 미실현 원가를 원문으로 표시한다.
 * 인자: props -> backend의 Decimal 수량·원가와 전략 상태
 * 반환값: 잔여가 있을 때 자산 안내 또는 null
 * 작성 날짜: 2026/09/08
 */
export function ResidualNotice({ quantity = '0', costBasis = '0', hasOpenPosition, isTrading }: ResidualNoticeProps) {
  // 서버 계약에서 검증한 Decimal 문자열을 float나 화면 자릿수로 반올림하지 않는다.
  if (!/[1-9]/.test(quantity)) return null;
  return (
    <aside role="status" aria-label="잔여 ETH 장부" className={styles.notice}>
      <strong>{!hasOpenPosition && !isTrading ? '전략 종료 · 잔여 ETH 있음' : '잔여 ETH 보유'}</strong>
      <div>잔여 자산: {quantity} ETH · 미실현 원가: {costBasis} USDT</div>
      <div>잔여 ETH는 계좌에 보유 중이며 가격 변동 위험이 남아 있습니다. 매도 완료 금액이나 실현손익으로 처리하지 않습니다.</div>
    </aside>
  );
}
