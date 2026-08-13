import { DashboardPage } from '../routes/dashboard';
import { TradeHistoryPage } from '../routes/trade-history';
import { AppHeader } from '../features/trading-control';
import { AppModalHost } from './components';
import {
    use_desktop_window_lifecycle,
    use_ui_application,
} from './hooks';
import {
    present_dashboard_props,
    present_trade_history_props,
} from './presenters';

import styles from './App.module.css';

/**
 * 함수 이름: App()
 * 기능: Binance Auto Trader의 facade runtime, 상단 상태, 현재 route와 전역 modal host를 연결한다.
 * 인자: 없음
 * 반환값: 애플리케이션 최상위 React 요소
 * 작성 날짜: 2026/08/12
 */
export function App() {
    const { controller, view_model } = use_ui_application();
    use_desktop_window_lifecycle(controller, view_model.app_exit.is_final);
    const dashboard_props = present_dashboard_props(view_model, controller);
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
                isCommandPending={view_model.trading.is_pending}
                isConnected={view_model.connection.is_online}
                isTrading={view_model.trading.is_trading}
                onStartRequested={() => controller.dispatch({ type: 'START_TRADING_CLICKED' })}
                onStopRequested={() => controller.dispatch({
                    type: 'STOP_TRADING_CLICKED',
                    has_open_position: view_model.trading.has_open_position,
                })}
            />

            {view_model.route === 'dashboard' ? (
                <DashboardPage {...dashboard_props} className={styles.dashboardRoute} />
            ) : (
                <TradeHistoryPage {...trade_history_props} />
            )}

            <AppModalHost controller={controller} viewModel={view_model} />
        </div>
    );
}
