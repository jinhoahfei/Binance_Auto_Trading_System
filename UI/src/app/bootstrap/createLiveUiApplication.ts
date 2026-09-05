import { UiApplicationFacade } from '../control';
import {
    BackendAdapterError,
    BackendUiAdapter,
    map_backend_snapshot,
    validate_connection_descriptor,
    type BackendConnectionDescriptor,
    type BackendUiAdapterDependencies,
} from '../../shared/api';
import type { LocalDateString } from '../../shared/contracts';
import type { UiApplicationFactory, UiApplicationRuntime } from './types';

/**
 * live bootstrap에서 transport test seam과 LocalDate source를 주입하는 옵션이다.
 */
export interface LiveUiApplicationOptions {
    readonly adapter_dependencies?: BackendUiAdapterDependencies;
    readonly today?: LocalDateString;
}

/**
 * 이미 생성한 live adapter를 재사용하는 snapshot hydration 옵션이다.
 */
export type LiveUiHydrationOptions = Pick<LiveUiApplicationOptions, 'today'>;

/**
 * snapshot-first bootstrap이 생성한 facade와 실제 loopback adapter 묶음이다.
 */
export interface LiveUiApplication extends UiApplicationRuntime {
    readonly command_adapter: BackendUiAdapter;
    readonly initial_session_id: string;
}

/**
 * 함수 이름: current_kst_date()
 * 기능: CSV/history actor 초기화에 사용할 Asia/Seoul LocalDate를 계산한다.
 * 인자: 없음
 * 반환값: YYYY-MM-DD 문자열
 * 작성 날짜: 2026/08/21
 */
function current_kst_date(): LocalDateString {
    return new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Seoul',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
    }).format(new Date());
}

/**
 * 함수 이름: create_live_ui_application()
 * 기능: ready 전체 snapshot을 먼저 검증한 뒤에만 facade와 WebSocket lifecycle을 생성한다.
 * 인자: descriptor -> native launch boundary의 loopback descriptor
 *      options -> transport test seam과 결정적 LocalDate
 * 반환값: cold hydration을 마쳤지만 아직 actor를 시작하지 않은 live runtime Promise
 * 작성 날짜: 2026/08/21
 */
export async function create_live_ui_application(
    descriptor: unknown,
    options: LiveUiApplicationOptions = {},
): Promise<LiveUiApplication> {
    const validated_descriptor = validate_connection_descriptor(descriptor);
    const command_adapter = new BackendUiAdapter(
        validated_descriptor,
        options.adapter_dependencies,
    );

    try {
        return await hydrate_live_ui_application(command_adapter, options);
    } catch (error) {
        // Startup failure에서는 facade를 만들지 않으며 token도 즉시 폐기한다.
        command_adapter.stop();
        throw error;
    }
}

/**
 * 함수 이름: hydrate_live_ui_application()
 * 기능: READY child의 같은 adapter로 snapshot hydration을 재시도하고 cold facade runtime을 만든다.
 * 인자: command_adapter -> descriptor token을 계속 소유하는 live adapter
 *      options -> 결정적 LocalDate source
 * 반환값: cold hydration을 마친 live runtime Promise
 * 작성 날짜: 2026/08/24
 */
export async function hydrate_live_ui_application(
    command_adapter: BackendUiAdapter,
    options: LiveUiHydrationOptions = {},
): Promise<LiveUiApplication> {
    const initial_snapshot = await command_adapter.load_snapshot();

    if (initial_snapshot.session_id !== command_adapter.session_id) {
        throw new BackendAdapterError(
            'SESSION_MISMATCH',
            'Backend snapshot session does not match the launch descriptor',
            false,
        );
    }

    const fixed_today = options.today;
    // Production은 매 dialog open에서 KST 날짜를 다시 읽고, fixture는 주입 날짜를 고정한다.
    const get_current_kst_date = fixed_today === undefined
        ? current_kst_date
        : () => fixed_today;
    const today = get_current_kst_date();
    const mapped_snapshot = map_backend_snapshot(initial_snapshot, today);
    const facade = new UiApplicationFacade(
        command_adapter,
        {
            ...mapped_snapshot.facade_options,
            get_current_kst_date,
        },
    );
    let is_active = false;

    return {
        command_adapter,
        facade,
        initial_session_id: initial_snapshot.session_id,
        environment: initial_snapshot.environment ?? null,  // 같은 backend session의 불변 실행 환경을 보존한다.
        load_binance_connection_status: (signal) => command_adapter.load_binance_connection_status(signal),
        activate: () => {
            if (is_active) {
                return;
            }

            is_active = true;
            facade.start();
            // 같은 coherent snapshot을 한 intent로 publish한 뒤 그 sequence에서 stream을 연다.
            facade.dispatch({
                type: 'BACKEND_SNAPSHOT_SYNCHRONIZED',
                snapshot: mapped_snapshot.server_snapshot,
            });
            command_adapter.start_live_events(initial_snapshot, {
                on_event: (intents) => {
                    intents.forEach((intent) => facade.dispatch(intent));
                },
                on_full_resync: (snapshot) => {
                    // Resync도 오래된 bootstrap 날짜 대신 같은 LocalDate source를 다시 읽는다.
                    const remapped_snapshot = map_backend_snapshot(
                        snapshot,
                        get_current_kst_date(),
                    );

                    facade.dispatch({
                        type: 'BACKEND_SNAPSHOT_SYNCHRONIZED',
                        snapshot: remapped_snapshot.server_snapshot,
                    });
                },
                on_reconnecting: (reason) => {
                    facade.dispatch({ type: 'API_DISCONNECTED', reason });
                },
                on_failure: (error) => {
                    // Online과 reconnecting 양쪽에서 동일하게 typed offline 최종 상태로 수렴시킨다.
                    facade.dispatch({ type: 'API_DISCONNECTED', reason: error.code });
                    facade.dispatch({ type: 'RECONNECT_FAILED', reason: error.message });
                },
            });
        },
        deactivate: () => {
            if (!is_active) {
                command_adapter.stop();
                return;
            }

            is_active = false;
            command_adapter.stop();
            facade.stop();
        },
    };
}

/**
 * 함수 이름: create_live_ui_application_factory()
 * 기능: snapshot-first로 준비한 한 live runtime을 React store의 명시적 sync factory로 감싼다.
 * 인자: application -> create_live_ui_application이 준비한 runtime
 *      on_activated -> facade와 event lifecycle이 시작된 직후 호출할 optional native replay hook
 * 반환값: App에 주입할 UiApplicationFactory
 * 작성 날짜: 2026/08/21
 */
export function create_live_ui_application_factory(
    application: LiveUiApplication,
    on_activated?: () => void,
): UiApplicationFactory {
    let has_activated = false;
    const strict_mode_safe_runtime: UiApplicationRuntime = {
        facade: application.facade,
        environment: application.environment ?? null,
        load_binance_connection_status: (signal) => application.command_adapter.load_binance_connection_status(signal),
        activate: () => {
            if (has_activated) {
                return;
            }

            // React commit에서 처음 activate될 때만 launch runtime을 소비한다.
            has_activated = true;
            application.activate();
            on_activated?.();  // Bootstrap 중 native event는 facade가 시작된 이 경계 뒤에만 replay한다.
        },
        deactivate: () => application.deactivate(),
    };

    return () => {
        if (has_activated) {
            throw new BackendAdapterError(
                'LIVE_RUNTIME_ALREADY_CONSUMED',
                'Live UI runtime has already been consumed for this launch',
                false,
            );
        }

        // StrictMode render double-call은 같은 cold runtime을 볼 수 있지만 actual remount는 fail closed한다.
        return strict_mode_safe_runtime;
    };
}

/**
 * Tauri command bridge가 반환하는 값을 문서화하는 public injection contract alias이다.
 */
export type LiveBackendConnectionDescriptor = BackendConnectionDescriptor;
