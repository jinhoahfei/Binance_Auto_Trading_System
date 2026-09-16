import { describe, expect, it } from 'vitest';
import { UiApplicationFacade } from '../control/UiApplicationFacade';
import { FakeUiCommandAdapter } from '../../shared/testing';

describe('UI shell Phase 13 Communication trace', () => {
    /** Communication Case 3 메시지 1.1.1의 중복 화면 전환 음성 경계를 검증한다. */
    it('test_duplicate_show_history_transition_is_idempotent: 중복 event가 route를 흔들지 않는다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-09-16',
        });

        facade.start();
        facade.dispatch({ type: 'SHOW_TRADE_HISTORY' });

        const first_snapshot = facade.get_snapshot();

        // 이미 상세 화면이면 같은 event는 dashboard 복귀나 modal mutation을 만들지 않아야 한다.
        expect(facade.dispatch({ type: 'SHOW_TRADE_HISTORY' })).toBe(false);

        const duplicate_snapshot = facade.get_snapshot();

        expect(duplicate_snapshot.context).toEqual(first_snapshot.context);
        expect(duplicate_snapshot.matches({
            ETIRE_UI_SYSTEM: { SCREEN: 'TRADING_DETAILS' },
        })).toBe(true);
        expect(facade.get_view_model().active_modal).toBeNull();

        facade.stop();
    });
});
