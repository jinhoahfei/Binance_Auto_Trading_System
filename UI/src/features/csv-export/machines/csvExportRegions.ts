import type { AnyStateMachine } from 'xstate';


/**
 * 함수 이름: csv_export_regions()
 * 기능: CSV 설정의 파일명 상태를 기본값·입력 중·확정으로 구성한다.
 * 인자: definition -> CSV 입력·검증·작업 machine 정의
 * 반환값: 루트에 포함할 CSV 상태 정의
 * 작성 날짜: 2026/09/16
 */
export function csv_export_regions(definition: AnyStateMachine) {
    const csv_config = definition.config as any;
    const filename = csv_config.states.editing.states.file_name;
    const committed = filename.states.committed;

    /**
     * 함수 이름: replace_committed_target()
     * 기능: 파일명 확정 전이의 target을 FILE_NAME_WRITTEN으로 재귀 변환한다.
     * 인자: value -> 검사할 상태·전이 설정 값
     * 반환값: 파일명 확정 target을 변환한 설정 값
     * 작성 날짜: 2026/09/16
     */
    const replace_committed_target = (value: any): any => {
        if (Array.isArray(value)) {
            return value.map(replace_committed_target);
        }

        if (!value || typeof value !== 'object') {
            return value;
        }

        return Object.fromEntries(Object.entries(value).map(([key, item]) => [
            key,
            key === 'target' && item === 'committed'
                ? 'FILE_NAME_WRITTEN'
                : replace_committed_target(item),
        ]));
    };

    return {
        ...csv_config,
        states: {
            ...csv_config.states,
            editing: {
                ...csv_config.states.editing,
                states: {
                    ...csv_config.states.editing.states,
                    file_name: {
                        ...filename,
                        initial: 'restore',
                        states: {
                            restore: {
                                always: [
                                    {
                                        guard: ({ context }: any) => context.file_name_committed,
                                        target: 'FILE_NAME_WRITTEN',
                                    },
                                    {
                                        target: 'DEFAULT_FILE_NAME',
                                    },
                                ],
                            },
                            DEFAULT_FILE_NAME: {
                                ...committed,
                                meta: {
                                    spec_ids: ['CR3-01', 'CR3-02'],
                                },
                            },
                            FILE_NAME_WRITTEN: {
                                ...committed,
                                meta: {
                                    spec_ids: ['CR3-07'],
                                },
                            },
                            editing: replace_committed_target(filename.states.editing),
                        },
                    },
                },
            },
        },
    };
}
