import { UiApplicationFacade } from '../control';
import { TRADE_HISTORY_SUMMARY_FIXTURE } from '../../features/trade-history';
import { DEFAULT_DASHBOARD_PROPS } from '../../routes/dashboard/dashboardFixture';
import { FakeUiCommandAdapter } from '../../shared/testing';
import {
    DEMO_HISTORY_TRADE_RECORDS,
    DEMO_REALTIME_INDICATORS,
    DEMO_RECENT_TRADE_RECORDS,
} from './demoFixtures';
import type { UiApplicationRuntime } from './types';

const DEMO_TODAY = '2026-06-29';
const DEMO_CSV_FILE_NAME = 'ETH_trade_history_260629.csv';

/**
 * fake command adapter와 facade를 함께 보관하는 데모 애플리케이션 구성 결과이다.
 */
export interface DemoUiApplication extends UiApplicationRuntime {
    readonly command_adapter: FakeUiCommandAdapter;
}

/**
 * 함수 이름: create_demo_ui_application()
 * 기능: backend 미연결 UI에서 Figma 기본 상태를 제공할 fake adapter와 facade를 생성한다.
 * 인자: 없음
 * 반환값: 수명주기를 아직 시작하지 않은 데모 애플리케이션 구성
 * 작성 날짜: 2026/08/12
 */
export function create_demo_ui_application(): DemoUiApplication {
    const command_adapter = new FakeUiCommandAdapter();
    command_adapter.trade_history = DEMO_HISTORY_TRADE_RECORDS;
    command_adapter.trade_history_summary = TRADE_HISTORY_SUMMARY_FIXTURE;
    command_adapter.exported_receipt = {
        file_path: `/Users/demo/Exports/${DEMO_CSV_FILE_NAME}`,
        exported_row_count: DEMO_HISTORY_TRADE_RECORDS.length,
    };

    const facade = new UiApplicationFacade(command_adapter, {
        today: DEMO_TODAY,
        csv_default_file_name: DEMO_CSV_FILE_NAME,
        chart_interval: DEFAULT_DASHBOARD_PROPS.chart.interval,
        chart_indicators: {
            bollinger_bands: DEFAULT_DASHBOARD_PROPS.chart.indicatorSettings?.bollingerBand ?? true,
            ema9: DEFAULT_DASHBOARD_PROPS.chart.indicatorSettings?.ema9 ?? true,
            volume: DEFAULT_DASHBOARD_PROPS.chart.indicatorSettings?.volume ?? false,
        },
        recommended_regime: DEFAULT_DASHBOARD_PROPS.regime.recommended,
        applied_regime: null,
        command_enabled: true,
        regime_metrics: DEFAULT_DASHBOARD_PROPS.regime.metrics,
        recent_trades: DEMO_RECENT_TRADE_RECORDS,
        history_records: DEMO_HISTORY_TRADE_RECORDS,
        scale_in_percentage: DEFAULT_DASHBOARD_PROPS.splitOrder.buyPercentage,
        scale_out_percentage: DEFAULT_DASHBOARD_PROPS.splitOrder.sellPercentage,
        account_strategy: DEFAULT_DASHBOARD_PROPS.account.strategy,
        account_asset: DEFAULT_DASHBOARD_PROPS.account.asset,
        trade_history_summary: TRADE_HISTORY_SUMMARY_FIXTURE,
    });

    return {
        command_adapter,
        facade,
        activate: () => {
            // Demo runtime은 actor 시작 뒤에만 결정적인 online fixture를 적용한다.
            facade.start();
            initialize_demo_ui_application(facade);
        },
        deactivate: () => facade.stop(),
    };
}

/**
 * 함수 이름: initialize_demo_ui_application()
 * 기능: 시작된 facade를 API online과 최근 체결 지표 기본 상태로 동기화한다.
 * 인자: facade -> actor 수명주기가 시작된 UI 애플리케이션 facade
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
export function initialize_demo_ui_application(facade: UiApplicationFacade): void {
    facade.dispatch({ type: 'API_CONNECTED', sequence: 1 });
    facade.dispatch({
        type: 'REALTIME_INDICATORS_UPDATED',
        indicators: DEMO_REALTIME_INDICATORS,
    });
}
