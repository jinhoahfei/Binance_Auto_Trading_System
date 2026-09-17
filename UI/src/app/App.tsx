import { lazy, Suspense } from 'react';

import { AppHeader } from '../features/trading-control';
import { use_realtime_chart_data } from '../features/price-chart';
import { AppModalHost } from './components';
import {
    use_binance_connection_status,
    use_desktop_window_lifecycle,
    use_ui_application,
} from './hooks';
import {
    present_dashboard_props,
    present_trade_history_props,
} from './presenters';
import type { UiApplicationFactory } from './bootstrap';

import styles from './App.module.css';
import { connection_recovery_message } from '../shared/api/connectionRecoveryMessage';

const DashboardPage = lazy(async () => {
    const route_module = await import('../routes/dashboard/DashboardPage');

    return { default: route_module.DashboardPage };
});

const TradeHistoryPage = lazy(async () => {
    const route_module = await import('../routes/trade-history/TradeHistoryPage');

    return { default: route_module.TradeHistoryPage };
});

/**
 * production main 또는 명시적 test가 App에 주입할 runtime factory이다.
 */
export interface AppProps {
    readonly applicationFactory: UiApplicationFactory;
}


/**
 * 함수 이름: App()
 * 기능: Binance Auto Trader의 Controller 런타임, 상단 상태, 현재 route와 전역 modal host를 연결한다.
 * 인자: applicationFactory -> production 또는 테스트 환경의 애플리케이션 생성 함수
 * 반환값: 애플리케이션 최상위 React 요소
 * 작성 날짜: 2026/08/20
 */
export function App({ applicationFactory }: AppProps) {
    // 앱 수명에 속하는 Controller와 서버·시세 구독을 연결한다.
    const { controller, view_model, environment, load_binance_connection_status } = use_ui_application(applicationFactory);
    const connection_details = use_binance_connection_status(
        load_binance_connection_status,
        !view_model.app_exit.is_final,
        view_model.connection.is_online,
    );
    const market_snapshot = use_realtime_chart_data({
        enabled: import.meta.env.MODE !== 'test' && !view_model.app_exit.is_final,
        limit: 1000,
        symbol: 'ETHUSDT',
    });
    use_desktop_window_lifecycle(controller, view_model.app_exit.is_final);

    // 동일한 화면 모델을 각 route의 표시 속성으로 변환한다.
    const dashboard_props = present_dashboard_props(view_model, controller, market_snapshot);
    const trade_history_props = present_trade_history_props(view_model, controller);

    // 종료가 끝난 경우에는 조작 화면 대신 종료 안내를 표시한다.
    if (view_model.app_exit.is_final) {
        return (
            <main className={styles.finalState}>
                <strong>프로그램 종료 준비가 완료되었습니다.</strong>
                <span>데스크톱 창을 닫아도 안전합니다.</span>
            </main>
        );
    }

    // 상단 상태, 현재 route와 공통 모달을 하나의 앱 화면으로 조립한다.
    return (
        <div className={styles.application}>
            <AppHeader
                environment={environment}
                connectionDetails={view_model.connection.is_online ? connection_details.connection_details : null}
                connectionDetailsError={!view_model.connection.is_online || connection_details.has_error}
                connectionCheckedAt={view_model.connection.is_online ? connection_details.checked_at_ms : null}
                hasOpenPosition={view_model.trading.has_open_position}
                isCommandPending={!view_model.connection.is_online || (view_model.trading.is_pending && view_model.trading.lifecycle_status !== 'reconciliation_required')}
                isConnected={view_model.connection.is_online}
                isTrading={view_model.trading.is_trading}
                onConnectionDetailsOpenChange={connection_details.set_is_open}
                onStartRequested={() => controller.dispatch({ type: 'START_TRADING_CLICKED' })}
                onStopRequested={() => controller.dispatch({
                    type: 'STOP_TRADING_CLICKED',
                    has_open_position: view_model.trading.has_open_position,
                })}
            />

            {view_model.connection.recovery != null && !view_model.connection.is_online && (
                <div className={styles.connectionRecovery} role="status" aria-live="polite">
                    <strong>{view_model.connection.recovery.phase === 'closing' ? '안전 종료 상태 확인 중'
                        : view_model.connection.recovery.error_code === 'UI_STATE_PUBLICATION_FAILED' ? '최신 화면 정보 복구 중' : '화면 연결 복구 중'}</strong>
                    <span>재시도 {view_model.connection.recovery.attempt}회 · 마지막 정상 수신 {
                        view_model.connection.recovery.last_received_at_ms === null ? '확인 중'
                            : new Date(view_model.connection.recovery.last_received_at_ms).toLocaleTimeString('ko-KR', { hour12: false })
                    }</span>
                    <span>{connection_recovery_message(view_model.connection.recovery.error_code)} 자동으로 최신 상태를 다시 받고 있습니다. 최신 지표를 확인할 때까지 새 명령을 사용할 수 없습니다.</span>
                </div>
            )}

            <Suspense
                fallback={(
                    <main
                        aria-busy="true"
                        aria-live="polite"
                        className={styles.routeLoading}
                    >
                        화면을 불러오는 중입니다.
                    </main>
                )}
            >
                {view_model.route === 'dashboard' ? (
                    <DashboardPage {...dashboard_props} className={styles.dashboardRoute} />
                ) : (
                    <TradeHistoryPage {...trade_history_props} />
                )}
            </Suspense>

            <AppModalHost controller={controller} viewModel={view_model} />
        </div>
    );
}
