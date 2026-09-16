import { assign, enqueueActions, setup } from 'xstate';

import type { UiCommandPort } from '../../shared/ports';
import type { UiApplicationFacadeOptions } from '../control/uiApplicationContracts';
import { create_feature_definitions } from './uiFeatureDefinitions';
import { UiRegionComposition } from './uiRegionComposition';
import { ui_command_executor } from './uiCommandExecutor';
import { csv_export_regions } from '../../features/csv-export/machines/csvExportRegions';
import { split_order_regions } from '../../features/split-order/machines/splitOrderRegions';
import { trading_regions } from '../../features/trading-control/machines/tradingRegions';
import { regime_regions } from '../../features/regime-selection/machines/regimeRegions';
import { history_filter_region } from '../../features/trade-history/machines/historyFilterRegions';
import type { FeatureContexts, UiApplicationContext, UiDomainEvent } from './uiApplicationTypes';


/**
 * 함수 이름: create_ui_application_machine()
 * 기능: 상단·화면·종료 Region과 명령 수명을 하나의 XState 루트로 구성한다.
 * 인자: command_port -> 비동기 명령 계약, options -> 기능별 초기 데이터와 설정
 * 반환값: 전체 UI 상태 계층을 실행하는 machine 정의
 * 작성 날짜: 2026/09/16
 */
export function create_ui_application_machine(command_port: UiCommandPort, options: UiApplicationFacadeOptions) {
    const definitions = create_feature_definitions(command_port, options);
    const composition = new UiRegionComposition(definitions);
    const trading = trading_regions(definitions.trading);
    const chart = composition.compile('chart', definitions.chart.config);
    const regime = composition.compile('regime', regime_regions(definitions.regime));
    const account = composition.compile('account_summary', definitions.account_summary.config);
    const recent = composition.compile('recent_orders', definitions.recent_orders.config);
    const summary = composition.compile('trade_history_summary', definitions.trade_history_summary.config);
    const query = composition.compile('trade_history', definitions.trade_history.config);
    const period = composition.compile('trade_history', history_filter_region('period'), 'history_period');
    const side = composition.compile('trade_history', history_filter_region('side'), 'history_side');
    const connection = composition.compile('connection', definitions.connection.config);
    const start = composition.compile('trading', trading.start, 'trading_start');
    const stop = composition.compile('trading', trading.stop, 'trading_stop');
    const exit = composition.compile('app_exit', definitions.app_exit.config);

    account.states.SPLIT_ORDER = split_order_regions(composition);

    const csv = composition.compile('csv_export', csv_export_regions(definitions.csv_export));

    for (const region of [chart, regime, account, recent, summary, query, period, side, connection, start, stop, exit, csv]) {
        composition.connect_commands(region);
    }

    return setup({
        types: {
            context: {} as UiApplicationContext,
            events: {} as UiDomainEvent,
        },
        actors: {
            ui_commands: ui_command_executor,
        },
    }).createMachine({
        id: 'uiApplicationMachine',
        initial: 'ETIRE_UI_SYSTEM',
        context: () => ({
            server_snapshot: null,
            features: composition.initial as FeatureContexts,
            requests: {},
            request_sequence: 0,
            summary_revision: 0,
            retained: undefined,
            retained_details: undefined,
            deferred: [],
        }),
        states: {
            ETIRE_UI_SYSTEM: {
                type: 'parallel',
                invoke: {
                    id: 'ui_commands',
                    src: 'ui_commands',
                },
                on: {
                    ...composition.fallback,
                    'ui.batch': {
                        actions: enqueueActions(({ event, enqueue }) => {
                            // 서버 snapshot을 공통 데이터에 먼저 반영한다.
                            // 뒤따르는 내부 이벤트까지 처리하여 Region을 동기화한 후
                            // 완성된 snapshot을 화면에 전달한다.
                            if (event.server_snapshot) {
                                enqueue.assign({
                                    server_snapshot: event.server_snapshot,
                                });
                            }

                            for (const item of event.events ?? []) {
                                enqueue.raise(item);
                            }
                        }),
                    },
                    'summary_revision.advance': {
                        actions: assign({
                            summary_revision: ({ context }) => context.summary_revision + 1,
                        }),
                    },
                },
                states: {
                    UPPER_STATUS_BAR: {
                        type: 'parallel',
                        states: {
                            API_DISPLAY: connection,
                            STOP_BUTTON: stop,
                            START_BUTTON: start,
                        },
                    },
                    SCREEN: {
                        initial: 'MAIN_SCREEN_WRAPPER',
                        states: {
                            MAIN_SCREEN_WRAPPER: {
                                id: 'MAIN_SCREEN_WRAPPER',
                                type: 'parallel',
                                entry: enqueueActions(({ context, enqueue }) => {

                                    /**
                                     * 함수 이름: is_main_screen_event()
                                     * 기능: 복귀 시 처리할 지연 이벤트가 메인 화면의 기능에 속하는지 검사한다.
                                     * 인자: event -> 화면 밖에서 보관한 내부 이벤트
                                     * 반환값: 메인 화면 소유 이벤트이면 true
                                     * 작성 날짜: 2026/09/16
                                     */
                                    const is_main_screen_event = (event: UiDomainEvent) => ['chart', 'regime', 'account_summary', 'split_order', 'recent_orders'].includes(event.owner ?? '');

                                    for (const event of context.deferred.filter(is_main_screen_event)) {
                                        enqueue.raise(event);
                                    }

                                    enqueue.assign({
                                        deferred: context.deferred.filter(event => !is_main_screen_event(event)),
                                    });
                                }),
                                exit: assign({
                                    retained: ({ self }) => self.getSnapshot().value,
                                }),
                                on: {
                                    'shell.SHOW_ALL_TRADING_DETAILS': {
                                        target: '#DETAILS_HISTORY',
                                    },
                                },
                                states: {
                                    HISTORY: {
                                        id: 'MAIN_SCREEN_HISTORY',
                                        type: 'history',
                                        history: 'deep',
                                        meta: {
                                            spec_ids: ['ES2-03', 'H*'],
                                        },
                                    },
                                    REGIME_PANEL: regime,
                                    DISPLAY_CHART: chart,
                                    DISPLAY_ACCOUNT_INFO: account,
                                    TRADER_PANEL: recent,
                                },
                                meta: {
                                    spec_ids: ['ES2-01', 'ES2-02'],
                                },
                            },
                            TRADING_DETAILS: {
                                type: 'parallel',
                                meta: {
                                    spec_ids: ['ES2-02', 'ES2-03'],
                                },
                                entry: enqueueActions(({ context, enqueue }) => {

                                    /**
                                     * 함수 이름: is_details_screen_event()
                                     * 기능: 복귀 시 처리할 지연 이벤트가 상세 화면의 기능에 속하는지 검사한다.
                                     * 인자: event -> 화면 밖에서 보관한 내부 이벤트
                                     * 반환값: 상세 화면 소유 이벤트이면 true
                                     * 작성 날짜: 2026/09/16
                                     */
                                    const is_details_screen_event = (event: UiDomainEvent) => ['csv_export', 'trade_history_summary'].includes(event.owner ?? '');

                                    for (const event of context.deferred.filter(is_details_screen_event)) {
                                        enqueue.raise(event);
                                    }

                                    enqueue.assign({
                                        deferred: context.deferred.filter(event => !is_details_screen_event(event)),
                                    });
                                }),
                                exit: assign({
                                    retained_details: ({ self }) => self.getSnapshot().value,
                                }),
                                on: {
                                    'shell.BACK_TO_MAIN_SCREEN': {
                                        target: '#MAIN_SCREEN_HISTORY',
                                    },
                                },
                                states: {
                                    HISTORY: {
                                        id: 'DETAILS_HISTORY',
                                        type: 'history',
                                        history: 'deep',
                                    },
                                    ACCOUNT_DETAILS: summary,
                                    PERIOD: {
                                        type: 'parallel',
                                        states: {
                                            selection: period,
                                            query,
                                        },
                                    },
                                    SIDE: side,
                                    CSV_EXPORT: csv,
                                },
                            },
                        },
                    },
                    EXIT: exit,
                },
            },
            UI_FINAL_STATE: {
                id: 'UI_FINAL_STATE',
                type: 'final',
                entry: assign({
                    requests: {},
                    deferred: [],
                }),
                meta: {
                    spec_ids: ['UI_FINAL_STATE'],
                },
            },
        },
    });
}
