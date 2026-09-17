// 분할 주문에서 사용하는 데이터 구조와 입력·표시 타입을 정의한다.

export type SplitOrderSide = 'buy' | 'sell';

export interface SplitOrderIntent {
    readonly percentage: number;
    readonly side: SplitOrderSide;
    readonly type: 'SPLIT_PERCENTAGE_REQUESTED';
}

export interface PercentSliderProps {
    readonly disabled?: boolean;
    readonly label: string;
    readonly onChange?: ((percentage: number) => void) | undefined;
    readonly side: SplitOrderSide;
    readonly value: number;
}

export interface SplitOrderControlsProps {
    readonly buyPercentage: number;
    readonly disabled?: boolean;
    readonly onIntent?: ((intent: SplitOrderIntent) => void) | undefined;
    readonly sellPercentage: number;
}
