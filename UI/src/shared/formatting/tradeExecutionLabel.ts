import type { TradeRecord } from '../contracts';


/**
 * 함수 이름: format_trade_execution_label()
 * 기능: 포지션 소유 전략을 보존하면서 체결 화면에는 실제 중지·수동 매도 사유를 표시한다.
 * 인자: trade -> 매수·매도 방향, 소유 전략, 청산 사유를 가진 거래 기록
 * 반환값: 최근 체결과 거래 내역에서 공통으로 사용할 표시 이름
 * 작성 날짜: 2026/09/27
 */
export function format_trade_execution_label(
    trade: Pick<TradeRecord, 'side' | 'strategy' | 'exit_reason'>,
): string {
    if (trade.side === 'sell') {
        if (trade.exit_reason === 'FORCE_SELL') return '강제매도';
        if (trade.exit_reason === 'EXTERNAL_MANUAL') return '외부 수동 매도';
    }
    return trade.strategy;
}
