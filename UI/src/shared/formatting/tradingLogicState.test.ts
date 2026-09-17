import { describe, it, expect } from 'vitest';
import { format_trading_logic_state } from './tradingLogicState';
import { create_backend_snapshot_fixture } from '../api/backendTestFixtures';
import { validate_backend_snapshot } from '../api/backendEventMapper';

describe('recovery state', () => {
    it('distinguishes prolonged recovery and blocked states and preserves STOP', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const trading = { ...create_backend_snapshot_fixture().trading, status: 'reconciliation_required' as const,
            recovery: { phase: 'market' as const, started_at: '2026-09-10T04:00:00Z', attempts: 2,
                block_reason: null, last_market_input_at: null, last_strategy_evaluation_at: null, prolonged: false } };

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(format_trading_logic_state(trading)).toBe('자동 복구 중');
        expect(format_trading_logic_state({ ...trading, recovery: { ...trading.recovery, prolonged: true } })).toBe('자동 복구 지연 · 60초 이상');
        expect(format_trading_logic_state({ ...trading, recovery: { ...trading.recovery, phase: 'blocked' } })).toBe('사용자 조치 필요');
        expect(format_trading_logic_state({ ...trading, status: 'terminated' })).toBe('자동매매 종료');
    });
    it('rejects malformed optional recovery metadata', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const snapshot = create_backend_snapshot_fixture();

        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        expect(() => validate_backend_snapshot({ ...snapshot, trading: { ...snapshot.trading, recovery: { phase: 'market', attempts: -1 } } })).toThrow();
        expect(() => validate_backend_snapshot(snapshot)).not.toThrow();
    });
});
