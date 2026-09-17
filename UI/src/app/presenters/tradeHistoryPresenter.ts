import {
    TRADE_HISTORY_ROWS_FIXTURE,
    type HistoryPeriod,
    type TradeRowViewModel,
    type TradeSideFilter,
} from '../../features/trade-history';
import type { TradeHistoryPageProps } from '../../routes/trade-history';
import type { TradeRecord } from '../../shared/contracts';
import { format_decimal_text, format_eth_quantity, format_quote_amount } from '../../shared/formatting';
import type { AppViewModel } from '../control';
import type { UiApplicationController } from '../runtime';

const HISTORY_ROW_BY_ID = new Map(
    TRADE_HISTORY_ROWS_FIXTURE.map((row) => [row.id, row]),
);

const EMPTY_HISTORY_STATE = {
    title: '거래 내역이 없습니다',
    description: '선택한 기간과 거래 구분에 해당하는 체결 기록이 없습니다.',
    suggestion: '필터를 변경하거나 자동매매를 시작한 뒤 다시 확인해 주세요.',
    actionLabel: '자동매매 화면으로 이동',
};

const LOADING_HISTORY_STATE = {
    title: '거래 내역을 불러오는 중입니다',
    description: '현재 선택한 기간과 거래 구분으로 최신 체결 기록을 조회하고 있습니다.',
    suggestion: '잠시만 기다려 주세요.',
    actionLabel: '조회 중',
};

const HISTORY_PERIOD_LABELS: Readonly<Record<AppViewModel['trade_history']['period'], string>> = {
    today: '오늘',
    last7days: '최근 7일',
    last30days: '최근 30일',
    all: '전체 기간',
};

const HISTORY_SIDE_LABELS: Readonly<Record<AppViewModel['trade_history']['side'], string>> = {
    all: '전체',
    buy: '매수',
    sell: '매도',
};


/**
 * 함수 이름: create_fee_display()
 * 기능: 실제 수수료 자산·금액을 반올림 없이 표시하고 quote 환산값을 구분한다.
 * 인자: trade_record -> 원 수수료와 quote 환산액을 가진 거래 record
 * 반환값: 수수료 셀의 원 금액과 선택적 환산 안내
 * 작성 날짜: 2026/09/10
 */
function create_fee_display(trade_record: TradeRecord): Pick<TradeRowViewModel, 'fee' | 'feeNote'> {
    /**
     * 함수 이름: exact_amount()
     * 기능: 소수 문자열의 불필요한 뒤쪽 0만 제거하고 부동소수 변환 없이 원 금액을 보존한다.
     * 인자: value -> 원 수수료 금액 문자열
     * 반환값: 불필요한 0과 끝 소수점을 제거한 문자열
     * 작성 날짜: 2026/09/17
     */
    const exact_amount = (value: string) => value.includes('.')
        ? value.replace(/0+$/u, '').replace(/\.$/u, '')
        : value;

    // 수수료 자산이 있으면 원래 자산의 정확한 문자열로 표시한다.
    if (trade_record.fee_amount !== undefined && trade_record.fee_asset !== undefined) {
        if (trade_record.fee_asset !== 'MIXED') {
            return {
                fee: `${exact_amount(trade_record.fee_amount)} ${trade_record.fee_asset}`,
                ...(trade_record.fee_asset === trade_record.quote_asset
                    ? {}
                    : { feeNote: `≈ ${exact_amount(trade_record.fee)} ${trade_record.quote_asset ?? 'USDT'}` }),
            };
        }

        return {
            fee: `${exact_amount(trade_record.fee)} ${trade_record.quote_asset ?? 'USDT'} 환산`,
            ...(trade_record.fee_note === undefined ? {} : { feeNote: trade_record.fee_note }),
        };
    }

    // 자산별 정보가 없는 기존 데이터는 quote 기준 표시로 처리한다.
    return {
        fee: format_quote_amount(trade_record.fee, trade_record.quote_asset),
        ...(trade_record.fee_note === undefined ? {} : { feeNote: trade_record.fee_note }),
    };
}


/**
 * 함수 이름: map_history_period_to_view()
 * 기능: actor의 기간 계약을 거래 내역 Boundary가 사용하는 표시 enum으로 변환한다.
 * 인자: period -> actor 기간 값
 * 반환값: HistoryFilters 표시 기간 값
 * 작성 날짜: 2026/08/12
 */
function map_history_period_to_view(
    period: AppViewModel['trade_history']['period'],
): HistoryPeriod {
    const period_map: Readonly<Record<AppViewModel['trade_history']['period'], HistoryPeriod>> = {
        today: 'TODAY',
        last7days: 'WEEKLY',
        last30days: 'MONTHLY',
        all: 'ALL',
    };

    return period_map[period];
}


/**
 * 함수 이름: map_view_period_to_history()
 * 기능: HistoryFilters 표시 enum을 actor의 공통 기간 계약으로 변환한다.
 * 인자: period -> 화면에서 선택한 기간
 * 반환값: facade HISTORY_PERIOD_SELECTED intent 값
 * 작성 날짜: 2026/08/12
 */
function map_view_period_to_history(
    period: HistoryPeriod,
): AppViewModel['trade_history']['period'] {
    const period_map: Readonly<Record<HistoryPeriod, AppViewModel['trade_history']['period']>> = {
        TODAY: 'today',
        WEEKLY: 'last7days',
        MONTHLY: 'last30days',
        ALL: 'all',
    };

    return period_map[period];
}


/**
 * 함수 이름: map_history_side_to_view()
 * 기능: actor의 거래 방향 계약을 HistoryFilters 표시 enum으로 변환한다.
 * 인자: side -> actor 거래 방향 값
 * 반환값: 화면 거래 방향 값
 * 작성 날짜: 2026/08/12
 */
function map_history_side_to_view(
    side: AppViewModel['trade_history']['side'],
): TradeSideFilter {
    const side_map: Readonly<Record<AppViewModel['trade_history']['side'], TradeSideFilter>> = {
        all: 'ALL',
        buy: 'BUY',
        sell: 'SELL',
    };

    return side_map[side];
}


/**
 * 함수 이름: map_view_side_to_history()
 * 기능: HistoryFilters 표시 enum을 actor의 공통 거래 방향 계약으로 변환한다.
 * 인자: side -> 화면에서 선택한 거래 방향
 * 반환값: facade HISTORY_SIDE_SELECTED intent 값
 * 작성 날짜: 2026/08/12
 */
function map_view_side_to_history(
    side: TradeSideFilter,
): AppViewModel['trade_history']['side'] {
    const side_map: Readonly<Record<TradeSideFilter, AppViewModel['trade_history']['side']>> = {
        ALL: 'all',
        BUY: 'buy',
        SELL: 'sell',
    };

    return side_map[side];
}


/**
 * 함수 이름: format_history_time()
 * 기능: actor ISO 시각을 상세 거래 표의 YY/MM/DD - HH:mm:ss KST 문자열로 변환한다.
 * 인자: occurred_at -> ISO 체결 시각
 * 반환값: 거래 표 시각 문자열
 * 작성 날짜: 2026/08/12
 */
function format_history_time(occurred_at: string): string {
    // 거래 시각을 지정한 시간대의 날짜·시각 조각으로 분리한다.
    const date_parts = new Intl.DateTimeFormat('en-CA', {
        year: '2-digit',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
        timeZone: 'Asia/Seoul',
    }).formatToParts(new Date(occurred_at));

    /**
     * 함수 이름: get_part()
     * 기능: KST 날짜의 지정 구성 요소를 찾고 없으면 두 자리 기본값을 반환한다.
     * 인자: part_type -> Intl 날짜 구성 요소 이름
     * 반환값: 해당 날짜 요소 또는 00
     * 작성 날짜: 2026/09/17
     */
    const get_part = (part_type: Intl.DateTimeFormatPartTypes) => (
        date_parts.find((part) => part.type === part_type)?.value ?? '00'
    );

    // 날짜와 시각 조각을 거래 내역의 고정 표시 형식으로 조립한다.
    return `${get_part('year')}/${get_part('month')}/${get_part('day')} - ${get_part('hour')}:${get_part('minute')}:${get_part('second')}`;
}


/**
 * 함수 이름: create_trade_row_view_model()
 * 기능: actor 거래 record를 Figma 행 fixture 또는 정규화된 기본 표 행으로 변환한다.
 * 인자: trade_record -> actor 거래 기록
 * 반환값: TradeTable 행 ViewModel
 * 작성 날짜: 2026/08/12
 */
function create_trade_row_view_model(trade_record: TradeRecord): TradeRowViewModel {
    // 기존 fixture 행은 보관된 화면 표시값을 그대로 사용한다.
    const fixture_row = trade_record.quote_asset === undefined
        ? HISTORY_ROW_BY_ID.get(trade_record.id)
        : undefined;

    if (fixture_row !== undefined) {
        return fixture_row;
    }

    // 실제 거래 데이터는 시각·금액·전략·수수료의 표시 계약으로 변환한다.
    return {
        id: trade_record.id,
        time: format_history_time(trade_record.occurred_at),
        side: trade_record.side.toUpperCase() as TradeRowViewModel['side'],
        regime: trade_record.regime,
        strategy: trade_record.exit_reason === 'EXTERNAL_MANUAL' ? '외부 수동 매도' : trade_record.strategy,
        entryPrice: trade_record.entry_price === null
            ? '-'
            : format_quote_amount(trade_record.entry_price, trade_record.quote_asset),
        executionPrice: format_quote_amount(trade_record.price, trade_record.quote_asset),
        quantity: format_eth_quantity(trade_record.quantity),
        orderAmount: format_quote_amount(trade_record.total, trade_record.quote_asset),
        ...create_fee_display(trade_record),
        previousBuyReturn: trade_record.profit_rate === null
            ? '-'
            : `${format_decimal_text(trade_record.profit_rate)}%`,
        realizedPnl: trade_record.realized_pnl === null
            ? '-'
            : format_quote_amount(trade_record.realized_pnl, trade_record.quote_asset),
    };
}


/**
 * 함수 이름: present_trade_history_props()
 * 기능: AppViewModel의 route actor 상태를 제어형 TradeHistoryPage props로 투영한다.
 * 인자: view_model -> facade의 최신 화면 모델, controller -> 사용자 intent 전달 controller
 * 반환값: 거래 내역 화면과 필터 Boundary props
 * 작성 날짜: 2026/08/12
 */
export function present_trade_history_props(
    view_model: AppViewModel,
    controller: UiApplicationController,
): TradeHistoryPageProps {
    // 조회 중·실패·빈 결과의 안내를 현재 조회 상태에서 선택한다.
    const is_empty = view_model.trade_history.status === 'empty';
    const is_failed = view_model.trade_history.status === 'failed';
    const is_loading = view_model.trade_history.is_loading;
    const status_label = is_loading
        ? '조회 중'
        : is_failed
            ? '조회 실패'
            : `체결 ${view_model.trade_history.records.length}건`;
    const empty_state = is_loading
        ? LOADING_HISTORY_STATE
        : is_failed
            ? {
                title: '거래 내역을 불러오지 못했습니다',
                description: view_model.trade_history.error?.message
                    ?? '거래 내역 조회 중 알 수 없는 오류가 발생했습니다.',
                suggestion: '잠시 후 다시 시도해 주세요.',
                actionLabel: '다시 시도',
            }
            : is_empty
                ? EMPTY_HISTORY_STATE
                : undefined;

    // 필터와 조회 결과를 표시 모델로 묶고 조작을 Controller에 연결한다.
    return {
        description: `${HISTORY_PERIOD_LABELS[view_model.trade_history.period]} · ${view_model.trade_history.symbol} · ${HISTORY_SIDE_LABELS[view_model.trade_history.side]} · ${status_label}`,
        summary: view_model.trade_history.summary,
        rows: is_loading || is_failed
            ? []
            : view_model.trade_history.records.map(create_trade_row_view_model),
        period: map_history_period_to_view(view_model.trade_history.period),
        side: map_history_side_to_view(view_model.trade_history.side),
        isLoading: is_loading,
        filtersDisabled: is_loading,
        ...(empty_state === undefined ? {} : { emptyState: empty_state }),
        onBack: () => controller.dispatch({ type: 'BACK_TO_DASHBOARD' }),
        onPeriodChange: (period) => controller.dispatch({
            type: 'HISTORY_PERIOD_SELECTED',
            period: map_view_period_to_history(period),
        }),
        onSideChange: (side) => controller.dispatch({
            type: 'HISTORY_SIDE_SELECTED',
            side: map_view_side_to_history(side),
        }),
        onExportCsv: () => controller.dispatch({ type: 'OPEN_CSV_EXPORT' }),
        ...(is_loading
            ? {}
            : {
                onStartTrading: () => {
                    if (is_failed) {
                        controller.dispatch({ type: 'REFRESH_TRADE_HISTORY' });

                        return;
                    }

                    controller.dispatch({ type: 'BACK_TO_DASHBOARD' });
                    if (!view_model.trading.is_trading) {
                        controller.dispatch({ type: 'START_TRADING_CLICKED' });
                    }
                },
            }),
    };
}
