import { createActor } from 'xstate';
import { describe, expect, it } from 'vitest';

import { create_ui_shell_machine } from './uiShellMachine';

describe('UI shell Phase 13 Communication trace', () => {
    /** Communication Case 3 메시지 1.1.1의 중복 화면 전환 음성 경계를 검증한다. */
    it('test_duplicate_show_history_transition_is_idempotent: 중복 event가 route를 흔들지 않는다', () => {
        const actor = createActor(create_ui_shell_machine());

        actor.start();
        actor.send({ type: 'SHOW_ALL_TRADING_DETAILS' });
        const first_snapshot = actor.getSnapshot();

        // 이미 상세 화면이면 같은 event는 dashboard 복귀나 modal mutation을 만들지 않아야 한다.
        actor.send({ type: 'SHOW_ALL_TRADING_DETAILS' });
        const duplicate_snapshot = actor.getSnapshot();
        expect(first_snapshot.context).toEqual({
            route: 'trade_history',
            active_modal: null,
        });
        expect(duplicate_snapshot.context).toEqual(first_snapshot.context);
        expect(duplicate_snapshot.matches({ navigation: 'trade_history' })).toBe(true);
        actor.stop();
    });
});
