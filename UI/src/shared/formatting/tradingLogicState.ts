import type { BackendTradingSnapshot } from '../contracts';

/**
 * 함수 이름: format_trading_logic_state()
 * 기능: 실제 실행 STM의 Case와 대기 상태를 두 전략 표시 영역이 공유할 문구로 변환한다.
 * 인자: trading -> 검증된 backend 거래 스냅샷
 * 반환값: 활성 Case 목록 또는 실행 전략이 없는 상태의 표시 문구
 * 작성 날짜: 2026/09/05
 */
export function format_trading_logic_state(trading: BackendTradingSnapshot): string {
    // 종료되거나 아직 시작하지 않은 세션에서는 이전 Case 이름을 실행 중으로 표시하지 않는다.
    if (trading.status === 'not_started') {
        return '매매 시작 전';
    }
    if (trading.status === 'terminated') {
        return '자동매매 종료';
    }

    // 서버가 실행 전략을 제공한 경우에만 Case 이름을 표시하고 병렬 신호 감시는 함께 표시한다.
    const active_logic = trading.active_logic;
    if (active_logic !== undefined && active_logic !== null) {
        if (active_logic.active_strategies.length > 0) {
            return active_logic.active_strategies.map((strategy) => {
                return strategy === 'CASE_B' ? 'Case_B' : 'Case_C';
            }).join(' / ');  // 배열의 canonical 순서를 유지해 두 표시 영역의 문구를 고정한다.
        }
        if (active_logic.root_state === 'LOWER_TOUCH_WATCH') {
            return '하단 밴드 대기';
        }
        if (active_logic.root_state === 'TRADE_MANAGEMENT') {
            return '신호 대기';
        }
    }

    // 구버전 서버의 누락된 전략을 REGIME이나 마지막 체결 전략으로 대체하지 않는다.
    if (trading.status === 'reconciliation_required') {
        return '주문 상태 확인 필요';
    }
    return trading.status === 'stopping' ? '중지 처리 중' : '전략 확인 대기';
}
