import { createActor, fromCallback, type AnyActorRef } from 'xstate';

import type { CommandMessage } from './uiApplicationTypes';

// 루트가 소유하는 I/O 작업의 시작·결과 전달·중단을 담당하는 actor 정의다.
// 화면 이동만으로 쓰기를 중단하지 않으며, 명시적 취소나 루트 종료 시 작업을 정리한다.
// Promise actor를 중단하면 결과 전달이 차단되지만 이미 제출된 쓰기가 취소되지는 않는다.
export const ui_command_executor = fromCallback<CommandMessage>(({ receive, sendBack: send_back }) => {
    const pending_actors = new Map<string, AnyActorRef>();

    receive(message => {
        pending_actors.get(message.key)?.stop();
        pending_actors.delete(message.key);

        if (message.type === 'cancel') {
            return;
        }

        const actor = createActor(message.logic, {
            input: message.input,
        });

        pending_actors.set(message.key, actor);
        actor.subscribe({
            next: snapshot => {
                if (snapshot.status !== 'done' || pending_actors.get(message.key) !== actor) {
                    return;
                }

                pending_actors.delete(message.key);
                send_back({
                    type: `command.${message.key}.done`,
                    token: message.token,
                    source: {
                        type: 'command.done',
                        output: snapshot.output,
                    },
                });
            },
            error: error => {
                if (pending_actors.get(message.key) !== actor) {
                    return;
                }

                pending_actors.delete(message.key);
                send_back({
                    type: `command.${message.key}.error`,
                    token: message.token,
                    source: {
                        type: 'command.error',
                        error,
                    },
                });
            },
        });
        actor.start();
    });

    return () => {
        pending_actors.forEach(actor => actor.stop());
        pending_actors.clear();
    };
});
