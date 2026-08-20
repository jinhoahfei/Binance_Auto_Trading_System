import {
    useCallback,
    useEffect,
    useRef,
    useState,
} from 'react';

import type { ChartInterval, PriceChartDataStatus } from '../types';
import {
    build_combined_kline_stream_url,
    load_all_klines,
    load_older_klines,
    merge_klines,
    parse_combined_kline_message,
    supported_chart_intervals,
    type KlinesByInterval,
    type NormalizedKline,
} from '../data';

const default_reconnect_delays = [1_000, 2_000, 5_000, 10_000, 30_000] as const;

export type WebSocketFactory = (url: string) => WebSocket;

export interface RealtimeChartDataSnapshot {
    readonly data_status: PriceChartDataStatus;
    readonly klines_by_interval: KlinesByInterval;
    readonly status_message: string | null;
    readonly symbol: string;
    readonly updated_at: number | null;
}

export interface ChartHistoryLoadState {
    readonly error_message: string | null;
    readonly is_exhausted: boolean;
    readonly is_loading: boolean;
}

export interface ChartHistoryLoadStateByInterval {
    readonly '1m': ChartHistoryLoadState;
    readonly '30m': ChartHistoryLoadState;
    readonly '4h': ChartHistoryLoadState;
    readonly '1d': ChartHistoryLoadState;
}

export interface RealtimeChartDataRuntime extends RealtimeChartDataSnapshot {
    readonly history_load_state_by_interval: ChartHistoryLoadStateByInterval;
    readonly load_earlier_klines: (interval: ChartInterval) => void;
}

export interface UseRealtimeChartDataOptions {
    readonly enabled?: boolean;
    readonly fetch_implementation?: typeof fetch;
    readonly history_page_limit?: number;
    readonly limit?: number;
    readonly reconnect_delays?: ReadonlyArray<number>;
    readonly symbol?: string;
    readonly web_socket_factory?: WebSocketFactory;
}

/**
 * 함수 이름: create_idle_history_load_state()
 * 기능: 아직 과거 페이지를 요청하지 않은 단일 주기의 초기 상태를 만든다.
 * 인자: 없음
 * 반환값: 대기 중인 과거 봉 로드 상태
 * 작성 날짜: 2026/08/20
 */
function create_idle_history_load_state(): ChartHistoryLoadState {
    return {
        error_message: null,
        is_exhausted: false,
        is_loading: false,
    };
}

/**
 * 함수 이름: create_history_load_state_by_interval()
 * 기능: UI가 지원하는 네 주기의 독립적인 과거 페이지 로드 상태를 만든다.
 * 인자: 없음
 * 반환값: 주기별 과거 봉 로드 상태
 * 작성 날짜: 2026/08/20
 */
function create_history_load_state_by_interval(): ChartHistoryLoadStateByInterval {
    return {
        '1m': create_idle_history_load_state(),
        '30m': create_idle_history_load_state(),
        '4h': create_idle_history_load_state(),
        '1d': create_idle_history_load_state(),
    };
}

/**
 * 함수 이름: create_empty_klines_by_interval()
 * 기능: UI가 지원하는 네 주기의 빈 시장 snapshot을 만든다.
 * 인자: 없음
 * 반환값: 주기별 빈 kline 목록
 * 작성 날짜: 2026/08/20
 */
function create_empty_klines_by_interval(): KlinesByInterval {
    return {
        '1m': [],
        '30m': [],
        '4h': [],
        '1d': [],
    };
}

/**
 * 함수 이름: create_buffer_by_interval()
 * 기능: REST 조회 중 WebSocket 봉을 보존할 주기별 mutable buffer를 만든다.
 * 인자: 없음
 * 반환값: 주기별 WebSocket kline buffer
 * 작성 날짜: 2026/08/20
 */
function create_buffer_by_interval(): Record<ChartInterval, Array<NormalizedKline>> {
    return {
        '1m': [],
        '30m': [],
        '4h': [],
        '1d': [],
    };
}

/**
 * 함수 이름: get_error_message()
 * 기능: 알 수 없는 오류를 사용자에게 표시할 안전한 메시지로 변환한다.
 * 인자: error -> 처리할 오류 값
 * 반환값: 오류 설명 문자열
 * 작성 날짜: 2026/08/20
 */
function get_error_message(error: unknown): string {
    return error instanceof Error ? error.message : '알 수 없는 시장 데이터 오류가 발생했습니다.';
}

/**
 * 함수 이름: is_abort_error()
 * 기능: fetch 실패가 정상적인 AbortController 취소인지 확인한다.
 * 인자: error -> 확인할 오류 값
 * 반환값: AbortError이면 true
 * 작성 날짜: 2026/08/20
 */
function is_abort_error(error: unknown): boolean {
    return error instanceof Error && error.name === 'AbortError';
}

/**
 * 함수 이름: has_any_klines()
 * 기능: 시장 snapshot에 하나 이상의 주기 데이터가 있는지 확인한다.
 * 인자: klines_by_interval -> 확인할 주기별 kline 목록
 * 반환값: 하나 이상의 kline이 있으면 true
 * 작성 날짜: 2026/08/20
 */
function has_any_klines(klines_by_interval: KlinesByInterval): boolean {
    return Object.values(klines_by_interval).some((klines) => klines.length > 0);
}

/**
 * 함수 이름: create_default_web_socket()
 * 기능: 브라우저 기본 WebSocket으로 Binance combined stream 연결을 만든다.
 * 인자: url -> 연결할 WebSocket URL
 * 반환값: 생성된 WebSocket
 * 작성 날짜: 2026/08/20
 */
function create_default_web_socket(url: string): WebSocket {
    return new WebSocket(url);
}

/**
 * 함수 이름: merge_klines_without_truncation()
 * 기능: 시간순 정렬과 동일 봉 덮어쓰기를 유지하면서 이미 적재한 과거 봉을 자르지 않고 병합한다.
 * 인자: existing_klines -> 우선순위가 낮은 기존 봉 목록
 *      incoming_klines -> 동일 key에서 우선할 신규 봉 목록
 * 반환값: 중복 제거되고 시간순으로 정렬된 전체 봉 목록
 * 작성 날짜: 2026/08/20
 */
function merge_klines_without_truncation(
    existing_klines: ReadonlyArray<NormalizedKline>,
    incoming_klines: ReadonlyArray<NormalizedKline>,
): ReadonlyArray<NormalizedKline> {
    return merge_klines(
        existing_klines,
        incoming_klines,
        Math.max(1, existing_klines.length + incoming_klines.length),
    );
}

/**
 * 함수 이름: merge_live_kline()
 * 기능: 현재 진행 봉 교체와 새 봉 append를 정렬 전체 재계산 없이 처리하고 예외 순서만 공통 병합한다.
 * 인자: existing_klines -> 시간순으로 적재된 기존 봉 목록
 *      incoming_kline -> WebSocket에서 받은 단일 최신 봉
 * 반환값: 이전 과거 이력을 보존한 시간순 봉 목록
 * 작성 날짜: 2026/08/20
 */
function merge_live_kline(
    existing_klines: ReadonlyArray<NormalizedKline>,
    incoming_kline: NormalizedKline,
): ReadonlyArray<NormalizedKline> {
    const latest_kline = existing_klines.at(-1);

    if (latest_kline === undefined) {
        return [incoming_kline];
    }
    if (latest_kline.symbol === incoming_kline.symbol
        && latest_kline.interval === incoming_kline.interval
        && latest_kline.open_time === incoming_kline.open_time) {
        return [...existing_klines.slice(0, -1), incoming_kline];
    }
    if (incoming_kline.open_time > latest_kline.open_time) {
        return [...existing_klines, incoming_kline];
    }

    return merge_klines_without_truncation(existing_klines, [incoming_kline]);
}

/**
 * 함수 이름: create_history_abort_controllers()
 * 기능: 네 주기가 서로 독립적으로 과거 REST 요청을 취소할 수 있는 controller map을 만든다.
 * 인자: 없음
 * 반환값: 주기별 nullable AbortController map
 * 작성 날짜: 2026/08/20
 */
function create_history_abort_controllers(): Record<ChartInterval, AbortController | null> {
    return {
        '1m': null,
        '30m': null,
        '4h': null,
        '1d': null,
    };
}

/**
 * 함수 이름: use_realtime_chart_data()
 * 기능: WebSocket을 먼저 열고 네 주기 REST 봉과 초기 buffer를 병합한 뒤 실시간 snapshot을 유지한다.
 * 인자: options -> symbol, 조회 개수, 네트워크 구현과 재연결 설정
 * 반환값: 주기별 kline, 연결 상태와 갱신 시각
 * 작성 날짜: 2026/08/20
 */
export function use_realtime_chart_data(
    options: UseRealtimeChartDataOptions = {},
): RealtimeChartDataRuntime {
    const enabled = options.enabled ?? true;
    const history_page_limit = options.history_page_limit ?? 1000;
    const limit = options.limit ?? 1000;
    const reconnect_delays = options.reconnect_delays ?? default_reconnect_delays;
    const symbol = (options.symbol ?? 'ETHUSDT').trim().toUpperCase();
    const [snapshot, set_snapshot] = useState<RealtimeChartDataSnapshot>(() => ({
        data_status: enabled ? 'loading' : 'idle',
        klines_by_interval: create_empty_klines_by_interval(),
        status_message: enabled ? 'Binance 실시간 봉을 동기화하고 있습니다.' : null,
        symbol,
        updated_at: null,
    }));
    const [history_load_state_by_interval, set_history_load_state_by_interval] = useState(
        create_history_load_state_by_interval,
    );
    const history_load_state_ref = useRef(history_load_state_by_interval);
    const load_earlier_klines_ref = useRef<(interval: ChartInterval) => void>(() => undefined);
    const snapshot_ref = useRef(snapshot);

    history_load_state_ref.current = history_load_state_by_interval;
    snapshot_ref.current = snapshot;

    /**
     * 함수 이름: load_earlier_klines()
     * 기능: 현재 effect가 소유한 안정적인 주기별 과거 봉 요청 handler를 호출한다.
     * 인자: interval -> 이전 봉을 요청할 chart 주기
     * 반환값: 없음
     * 작성 날짜: 2026/08/20
     */
    const load_earlier_klines = useCallback((interval: ChartInterval): void => {
        load_earlier_klines_ref.current(interval);
    }, []);

    useEffect(() => {
        if (!enabled) {
            const idle_history_load_state = create_history_load_state_by_interval();

            history_load_state_ref.current = idle_history_load_state;
            load_earlier_klines_ref.current = () => undefined;
            set_history_load_state_by_interval(idle_history_load_state);
            set_snapshot({
                data_status: 'idle',
                klines_by_interval: create_empty_klines_by_interval(),
                status_message: null,
                symbol,
                updated_at: null,
            });
            return undefined;
        }

        let active_abort_controller: AbortController | null = null;
        let active_socket: WebSocket | null = null;
        let connection_version = 0;
        let is_disposed = false;
        let reconnect_attempt = 0;
        let reconnect_timer: ReturnType<typeof setTimeout> | null = null;
        const history_abort_controllers = create_history_abort_controllers();
        const web_socket_factory = options.web_socket_factory
            ?? (typeof WebSocket === 'undefined' ? null : create_default_web_socket);
        const initial_history_load_state = create_history_load_state_by_interval();

        history_load_state_ref.current = initial_history_load_state;
        set_history_load_state_by_interval(initial_history_load_state);

        /**
         * 함수 이름: replace_history_load_state()
         * 기능: 다른 주기의 상태를 보존하면서 한 주기의 과거 페이지 상태를 즉시 교체한다.
         * 인자: interval -> 교체할 chart 주기
         *      next_state -> 새 과거 페이지 상태
         * 반환값: 없음
         * 작성 날짜: 2026/08/20
         */
        function replace_history_load_state(
            interval: ChartInterval,
            next_state: ChartHistoryLoadState,
        ): void {
            const next_state_by_interval: ChartHistoryLoadStateByInterval = {
                ...history_load_state_ref.current,
                [interval]: next_state,
            };

            history_load_state_ref.current = next_state_by_interval;
            set_history_load_state_by_interval(next_state_by_interval);
        }

        /**
         * 함수 이름: abort_history_requests()
         * 기능: 진행 중인 모든 주기의 과거 REST 요청을 취소하고 loading 표시를 정리한다.
         * 인자: update_state -> 화면의 loading 상태도 함께 정리할지 여부
         * 반환값: 없음
         * 작성 날짜: 2026/08/20
         */
        function abort_history_requests(update_state: boolean): void {
            supported_chart_intervals.forEach((interval) => {
                history_abort_controllers[interval]?.abort();
                history_abort_controllers[interval] = null;

                if (update_state && history_load_state_ref.current[interval].is_loading) {
                    replace_history_load_state(interval, {
                        ...history_load_state_ref.current[interval],
                        is_loading: false,
                    });
                }
            });
        }

        /**
         * 함수 이름: load_earlier_klines_for_interval()
         * 기능: 선택 주기의 현재 최저 open time 이전 페이지를 조회해 기존 실시간 봉 우선으로 prepend한다.
         * 인자: interval -> 이전 봉을 조회할 chart 주기
         * 반환값: 과거 페이지 요청 완료 Promise
         * 작성 날짜: 2026/08/20
         */
        async function load_earlier_klines_for_interval(interval: ChartInterval): Promise<void> {
            const current_history_state = history_load_state_ref.current[interval];
            const current_snapshot = snapshot_ref.current;
            const oldest_kline = current_snapshot.klines_by_interval[interval][0];

            if (is_disposed
                || history_abort_controllers[interval] !== null
                || current_history_state.is_exhausted
                || oldest_kline === undefined
                || current_snapshot.symbol !== symbol) {
                return;
            }

            const request_abort_controller = new AbortController();
            const request_connection_version = connection_version;
            const before_open_time = oldest_kline.open_time;

            history_abort_controllers[interval] = request_abort_controller;
            replace_history_load_state(interval, {
                error_message: null,
                is_exhausted: false,
                is_loading: true,
            });

            try {
                const older_klines = await load_older_klines(
                    symbol,
                    interval,
                    before_open_time,
                    {
                        limit: history_page_limit,
                        signal: request_abort_controller.signal,
                        ...(options.fetch_implementation === undefined
                            ? {}
                            : { fetch_implementation: options.fetch_implementation }),
                    },
                );

                if (is_disposed
                    || request_connection_version !== connection_version
                    || history_abort_controllers[interval] !== request_abort_controller) {
                    return;
                }

                const strictly_older_klines = older_klines.filter((kline) => {
                    return kline.open_time < before_open_time;
                });
                const has_older_progress = strictly_older_klines.length > 0;

                if (has_older_progress) {
                    set_snapshot((latest_snapshot) => {
                        if (latest_snapshot.symbol !== symbol) {
                            return latest_snapshot;
                        }

                        const next_snapshot: RealtimeChartDataSnapshot = {
                            ...latest_snapshot,
                            klines_by_interval: {
                                ...latest_snapshot.klines_by_interval,
                                [interval]: merge_klines_without_truncation(
                                    strictly_older_klines,
                                    latest_snapshot.klines_by_interval[interval],
                                ),
                            },
                        };

                        snapshot_ref.current = next_snapshot;
                        return next_snapshot;
                    });
                }

                replace_history_load_state(interval, {
                    error_message: null,
                    is_exhausted: !has_older_progress
                        || older_klines.length < history_page_limit,
                    is_loading: false,
                });
            } catch (error: unknown) {
                if (is_disposed
                    || request_connection_version !== connection_version
                    || history_abort_controllers[interval] !== request_abort_controller
                    || is_abort_error(error)) {
                    return;
                }

                replace_history_load_state(interval, {
                    error_message: `이전 ${interval} 봉을 불러오지 못했습니다: ${get_error_message(error)}`,
                    is_exhausted: false,
                    is_loading: false,
                });
            } finally {
                if (history_abort_controllers[interval] === request_abort_controller) {
                    history_abort_controllers[interval] = null;
                }
            }
        }

        /**
         * 함수 이름: request_earlier_klines()
         * 기능: UI callback을 reject되지 않는 주기별 과거 페이지 요청으로 연결한다.
         * 인자: interval -> 이전 봉을 요청할 chart 주기
         * 반환값: 없음
         * 작성 날짜: 2026/08/20
         */
        function request_earlier_klines(interval: ChartInterval): void {
            void load_earlier_klines_for_interval(interval);
        }

        load_earlier_klines_ref.current = request_earlier_klines;

        /**
         * 함수 이름: schedule_reconnect()
         * 기능: 중복 timer 없이 제한된 지수형 지연으로 전체 시장 snapshot 초기화를 다시 시도한다.
         * 인자: reason -> 재연결 상태에 표시할 사유
         * 반환값: 없음
         * 작성 날짜: 2026/08/20
         */
        function schedule_reconnect(reason: string): void {
            if (is_disposed || reconnect_timer !== null || web_socket_factory === null) {
                return;
            }

            const delay_index = Math.min(reconnect_attempt, reconnect_delays.length - 1);
            const reconnect_delay = reconnect_delays[delay_index] ?? 30_000;

            reconnect_attempt += 1;
            set_snapshot((current_snapshot) => ({
                ...current_snapshot,
                data_status: has_any_klines(current_snapshot.klines_by_interval)
                    ? 'reconnecting'
                    : 'error',
                status_message: `${reason} ${Math.ceil(reconnect_delay / 1_000)}초 후 다시 연결합니다.`,
            }));
            reconnect_timer = setTimeout(() => {
                reconnect_timer = null;
                initialize_market_data();
            }, reconnect_delay);
        }

        /**
         * 함수 이름: initialize_market_data()
         * 기능: combined WebSocket buffer를 먼저 시작하고 REST 네 주기 봉을 조회·병합한다.
         * 인자: 없음
         * 반환값: 초기화 완료 Promise
         * 작성 날짜: 2026/08/20
         */
        async function initialize_market_data(): Promise<void> {
            abort_history_requests(true);
            connection_version += 1;
            const current_connection_version = connection_version;
            const buffered_klines = create_buffer_by_interval();
            let historical_data_is_ready = false;
            let socket_is_open = false;

            active_abort_controller?.abort();
            active_socket?.close(1_000, 'Binance 시장 데이터 연결을 갱신합니다.');
            active_abort_controller = new AbortController();
            set_snapshot((current_snapshot) => {
                const retained_klines = current_snapshot.symbol === symbol
                    ? current_snapshot.klines_by_interval
                    : create_empty_klines_by_interval();
                const next_snapshot: RealtimeChartDataSnapshot = {
                    ...current_snapshot,
                    data_status: has_any_klines(retained_klines) ? 'reconnecting' : 'loading',
                    klines_by_interval: retained_klines,
                    status_message: 'Binance 실시간 봉을 동기화하고 있습니다.',
                    symbol,
                };

                snapshot_ref.current = next_snapshot;
                return next_snapshot;
            });

            if (web_socket_factory !== null) {
                try {
                    const socket = web_socket_factory(build_combined_kline_stream_url(symbol));

                    active_socket = socket;
                    socket.onopen = () => {
                        if (is_disposed || current_connection_version !== connection_version) {
                            return;
                        }

                        socket_is_open = true;
                        reconnect_attempt = 0;

                        if (historical_data_is_ready) {
                            set_snapshot((current_snapshot) => ({
                                ...current_snapshot,
                                data_status: 'live',
                                status_message: null,
                            }));
                        }
                    };
                    socket.onmessage = (event) => {
                        if (is_disposed || current_connection_version !== connection_version) {
                            return;
                        }

                        try {
                            const incoming_kline = parse_combined_kline_message(event.data);

                            if (!historical_data_is_ready) {
                                buffered_klines[incoming_kline.interval] = [
                                    ...merge_klines(
                                        buffered_klines[incoming_kline.interval],
                                        [incoming_kline],
                                        limit,
                                    ),
                                ];
                                return;
                            }

                            set_snapshot((current_snapshot) => ({
                                ...current_snapshot,
                                data_status: 'live',
                                klines_by_interval: {
                                    ...current_snapshot.klines_by_interval,
                                    [incoming_kline.interval]: merge_live_kline(
                                        current_snapshot.klines_by_interval[incoming_kline.interval],
                                        incoming_kline,
                                    ),
                                },
                                status_message: null,
                                updated_at: Date.now(),
                            }));
                        } catch (error: unknown) {
                            set_snapshot((current_snapshot) => ({
                                ...current_snapshot,
                                status_message: `잘못된 Binance 봉을 건너뛰었습니다: ${get_error_message(error)}`,
                            }));
                        }
                    };
                    socket.onerror = () => {
                        if (is_disposed || current_connection_version !== connection_version) {
                            return;
                        }

                        schedule_reconnect('Binance WebSocket 연결에 오류가 발생했습니다.');
                    };
                    socket.onclose = () => {
                        if (is_disposed || current_connection_version !== connection_version) {
                            return;
                        }

                        socket_is_open = false;
                        schedule_reconnect('Binance WebSocket 연결이 종료되었습니다.');
                    };
                } catch (error: unknown) {
                    schedule_reconnect(`Binance WebSocket을 시작하지 못했습니다: ${get_error_message(error)}`);
                }
            }

            try {
                const historical_klines = await load_all_klines(symbol, {
                    limit,
                    signal: active_abort_controller.signal,
                    ...(options.fetch_implementation === undefined
                        ? {}
                        : { fetch_implementation: options.fetch_implementation }),
                });

                if (is_disposed || current_connection_version !== connection_version) {
                    return;
                }

                historical_data_is_ready = true;
                supported_chart_intervals.forEach((interval) => {
                    if (historical_klines[interval].length < limit) {
                        replace_history_load_state(interval, {
                            ...history_load_state_ref.current[interval],
                            is_exhausted: true,
                        });
                    }
                });
                set_snapshot((current_snapshot) => {
                    const current_klines = current_snapshot.symbol === symbol
                        ? current_snapshot.klines_by_interval
                        : create_empty_klines_by_interval();
                    const merged_klines = Object.fromEntries(
                        supported_chart_intervals.map((interval) => {
                            const historical_and_current = merge_klines_without_truncation(
                                historical_klines[interval],
                                current_klines[interval],
                            );

                            return [
                                interval,
                                merge_klines_without_truncation(
                                    historical_and_current,
                                    buffered_klines[interval],
                                ),
                            ] as const;
                        }),
                    ) as unknown as KlinesByInterval;
                    const next_snapshot: RealtimeChartDataSnapshot = {
                        data_status: socket_is_open
                            ? 'live'
                            : web_socket_factory === null
                                ? 'error'
                                : 'reconnecting',
                        klines_by_interval: merged_klines,
                        status_message: socket_is_open
                            ? null
                            : web_socket_factory === null
                                ? '이 실행 환경에서는 실시간 WebSocket을 사용할 수 없습니다.'
                                : '과거 봉을 표시하며 실시간 연결을 기다리고 있습니다.',
                        symbol,
                        updated_at: Date.now(),
                    };

                    snapshot_ref.current = next_snapshot;
                    return next_snapshot;
                });
            } catch (error: unknown) {
                if (is_disposed || current_connection_version !== connection_version || is_abort_error(error)) {
                    return;
                }

                const error_message = get_error_message(error);

                set_snapshot((current_snapshot) => ({
                    ...current_snapshot,
                    data_status: 'error',
                    status_message: `Binance 과거 봉을 불러오지 못했습니다: ${error_message}`,
                }));
                schedule_reconnect('시장 데이터 초기화에 실패했습니다.');
            }
        }

        initialize_market_data();

        return () => {
            is_disposed = true;
            connection_version += 1;
            active_abort_controller?.abort();
            abort_history_requests(false);
            load_earlier_klines_ref.current = () => undefined;

            if (reconnect_timer !== null) {
                clearTimeout(reconnect_timer);
            }
            if (active_socket !== null) {
                active_socket.onclose = null;
                active_socket.onerror = null;
                active_socket.onmessage = null;
                active_socket.onopen = null;
                active_socket.close(1_000, '가격 차트 구독을 종료합니다.');
            }
        };
    }, [
        enabled,
        history_page_limit,
        limit,
        options.fetch_implementation,
        options.web_socket_factory,
        reconnect_delays,
        symbol,
    ]);

    return {
        ...snapshot,
        history_load_state_by_interval,
        load_earlier_klines,
    };
}
