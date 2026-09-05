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
 * 기능: Binance Auto Trader의 facade runtime, 상단 상태, 현재 route와 전역 modal host를 연결한다.
 * 인자: 없음
 * 반환값: 애플리케이션 최상위 React 요소
 * 작성 날짜: 2026/08/20
 */
export function App({ applicationFactory }: AppProps) {
    const { controller, view_model, load_binance_connection_status } = use_ui_application(applicationFactory);
    const connection_details = use_binance_connection_status(
        load_binance_connection_status,
        !view_model.app_exit.is_final,
    );
    const market_snapshot = use_realtime_chart_data({
        enabled: import.meta.env.MODE !== 'test' && !view_model.app_exit.is_final,
        limit: 1000,
        symbol: 'ETHUSDT',
    });
    use_desktop_window_lifecycle(controller, view_model.app_exit.is_final);
    const dashboard_props = present_dashboard_props(view_model, controller, market_snapshot);
    const trade_history_props = present_trade_history_props(view_model, controller);

    if (view_model.app_exit.is_final) {
        return (
            <main className={styles.finalState}>
                <strong>프로그램 종료 준비가 완료되었습니다.</strong>
                <span>데스크톱 창을 닫아도 안전합니다.</span>
            </main>
        );
    }

    return (
        <div className={styles.application}>
            <AppHeader
                connectionDetails={connection_details.connection_details}
                connectionDetailsError={connection_details.has_error}
                hasOpenPosition={view_model.trading.has_open_position}
                isCommandPending={view_model.trading.is_pending}
                isConnected={view_model.connection.is_online}
                isTrading={view_model.trading.is_trading}
                onConnectionDetailsOpenChange={connection_details.set_is_open}
                onStartRequested={() => controller.dispatch({ type: 'START_TRADING_CLICKED' })}
                onStopRequested={() => controller.dispatch({
                    type: 'STOP_TRADING_CLICKED',
                    has_open_position: view_model.trading.has_open_position,
                })}
            />

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
