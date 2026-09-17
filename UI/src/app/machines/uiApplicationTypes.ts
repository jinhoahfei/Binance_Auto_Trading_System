// UI 루트 상태 머신의 내부 데이터와 명령 실행부의 메시지 계약을 정의한다.
/**
 * 파일 역할: UI 루트 상태 머신의 내부 데이터와 명령 실행부의 메시지 계약을 정의한다.
 * 기능별 context, 내부 이벤트, 비동기 요청, 화면 복귀 정보와 루트 snapshot을 표현한다.
 * 외부 입력·화면 모델 계약은 uiApplicationContracts.ts에 정의한다.
 */

import type { SnapshotFrom, StateValue } from 'xstate';

import type { UIEvaluationInput } from './uiActions';

import type { create_feature_definitions } from './uiFeatureDefinitions';
import type { UiServerOwnedSnapshot } from '../control/uiApplicationContracts';

/** 기능별 상태 정의를 만드는 함수의 반환 타입에서 기능 이름과 context 타입을 도출한다. */
export type FeatureDefinitions = ReturnType<typeof create_feature_definitions>;
export type FeatureKey = keyof FeatureDefinitions;
export type FeatureContexts = { [K in FeatureKey]: SnapshotFrom<FeatureDefinitions[K]>['context'] };

/** 입력 router와 명령 실행부가 루트에 전달하는 이벤트 및 내부 동기화·복귀 이벤트이다. */
export interface UiDomainEvent {
    readonly type: string;
    // 기능별 가드·Action에 전달할 원래 이벤트와 데이터다.
    readonly source?: {
        readonly type: string;
        readonly [key: string]: unknown;
    };
    // ui.batch가 한 번의 입력 처리 안에서 순서대로 전달할 내부 이벤트 목록이다.
    readonly events?: readonly UiDomainEvent[];
    // 명령 결과를 제출 당시의 요청과 대조하여 오래된 결과를 구분한다.
    readonly token?: number;
    // 비활성 화면에서 보관한 복귀 이벤트의 소유 기능과 중복 대체 기준이다.
    readonly owner?: FeatureKey;
    readonly resume_key?: string;
    readonly server_snapshot?: UiServerOwnedSnapshot;
    readonly evaluation?: UIEvaluationInput & { readonly previous_value: StateValue };
}

/** 명령 key별 최신 요청 식별자와 처리 상태로, 명령 결과의 유효성을 판단하는 데 사용한다. */
export interface UiRequest {
    readonly token: number;
    readonly status: 'pending' | 'done' | 'error';
}

/** 모든 Region이 공유하는 서버 데이터·기능별 데이터·작업 및 화면 복귀 정보이다. */
export interface UiApplicationContext {
    readonly evaluation: UIEvaluationInput & { readonly previous_value?: StateValue };
    readonly server_snapshot: UiServerOwnedSnapshot | null;
    readonly features: FeatureContexts;
    readonly requests: Readonly<Record<string, UiRequest>>;
    readonly request_sequence: number;
    // 상세 조회가 반환한 요약이 최신 서버 갱신을 덮어쓰지 않도록 비교하는 버전이다.
    readonly summary_revision: number;
    // 화면을 떠날 때 보관한 루트 상태값으로, 비활성 화면의 표시 상태를 조회할 때 사용한다.
    readonly retained: StateValue | undefined;
    readonly retained_details: StateValue | undefined;
    // 비활성 화면에서 결과를 반영한 뒤, 화면 복귀 시 실행할 상태 전이 이벤트를 보관한다.
    readonly deferred: readonly UiDomainEvent[];
}

/** Facade가 공개하는 실제 루트의 활성 상태값·공통 context·실행 상태와 상태 일치 검사 계약이다. */
export interface UiApplicationSnapshot {
    readonly value: StateValue;
    readonly context: UiApplicationContext;
    readonly status: 'active' | 'done' | 'error' | 'stopped';
    matches(value: StateValue): boolean;
}
