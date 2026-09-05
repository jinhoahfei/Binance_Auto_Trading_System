import type { UiApplicationFacade } from '../control';
import type { BackendBinanceConnectionStatus } from '../../shared/contracts';

/**
 * demo와 live bootstrap이 store에 제공하는 공통 UI 애플리케이션 수명주기이다.
 */
export interface UiApplicationRuntime {
    readonly facade: UiApplicationFacade;
    readonly load_binance_connection_status?: (
        signal?: AbortSignal,
    ) => Promise<BackendBinanceConnectionStatus>;
    activate(): void;
    deactivate(): void;
}

/**
 * React store가 새 mount 수명주기에 사용할 runtime을 생성하는 factory이다.
 */
export type UiApplicationFactory = () => UiApplicationRuntime;
